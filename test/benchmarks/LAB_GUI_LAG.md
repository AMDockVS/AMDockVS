# Prueba de lag de la GUI en el laboratorio (fases A y B, plan 10 de ms_flow)

Esta prueba mide cuánto se retrasa el hilo de la GUI mientras el motor hace docking e importa. En la fase B, ms_flow corre el motor en un proceso hijo (`--engine-host process`, el valor por defecto); `--engine-host inline` lo deja en el proceso de la app, como en la fase A. Hay que correrla en el Threadripper con 16, 32 y 64 workers, en los dos modos.

**Criterio:** con 64 workers, p99 < 50 ms y máximo < 500 ms.

## 1. Instalar las ramas

En el entorno de la app del lab (el que usa `amdockvs`):

```bash
git clone https://github.com/AMDockVS/AMDockVS.git && cd AMDockVS   # o `git fetch` si ya está
git switch feat/engine-child-process && git pull   # sonda + fase B
pip install -e . --no-deps
pip install --no-deps --force-reinstall "git+https://github.com/MolSuite/ms_flow@feat/engine-child-process"
# Fase 0 del monitor (solo afecta a --window):
pip install --no-deps --force-reinstall "git+https://github.com/MolSuite/ms_components@perf/monitor-tasks-diff"
```

Comprobar con `pip show ms_flow` que la ubicación es la de la rama. `python -c "import ms_flow._api_impl.engine_host"` no debe fallar.

## 2. Preparar

- **Un SDF de moléculas pequeñas** (2D vale), por ejemplo `Enamine_H100K.sdf`. La sonda toma de él los ligandos del docking y lo repite hasta `--big-gb` para el import.
- **Un disco con espacio** para `--work`: `--big-gb` más unos 5 GB. Todo se borra al terminar, salvo `--keep`.
- **No hace falta abrir la app ni tener sesión gráfica:** sin `DISPLAY`, corre offscreen.
- **Máquina libre:** cerrar otras cargas pesadas mientras corre.

## 3. Correr

Desde la carpeta del repo, una corrida por número de workers. Son seis corridas de ~5 min cada una, más la preparación. La primera escribe el SDF grande (`$WORK/gui_lag_big_60gb.sdf`, unos minutos) y las demás lo reutilizan; antes lo reescribía cada corrida, por eso el bucle pasaba de 40 min:

```bash
LIB=/ruta/a/Enamine_H100K.sdf
WORK=/disco/grande/gui_lag
mkdir -p $WORK
for host in process inline; do
  for cpus in 0-15 0-31 0-63; do
    taskset -c $cpus python test/benchmarks/bench_gui_lag.py --library $LIB --work $WORK \
        --window --big-gb 60 --engine-host $host --out $WORK/lag.jsonl
  done
done
```

Opcional: una corrida con `--switch-ms 5` y 64 workers sirve para ver cuánto aporta el switch interval de la fase 0.

## 4. Qué devuelve

Por cada fase (`idle`, `dock`, `dock+import`) imprime una línea:

| campo | significado |
|---|---|
| `p50_ms`, `p99_ms`, `max_ms` | retraso de los ticks de 20 ms |
| `max_at_s` | segundo de la fase en que llegó el peor tick |
| `dock_chunks_per_s` | ritmo del docking |
| `engine_host` | `process` (fase B) o `inline` (fase A) |
| `dock.tasks_running`, `import.tasks_running` | tareas de cada job; si su suma cae muy por debajo de los workers, el despacho se está frenando |

- Al final escribe una línea JSON y la añade a `lag.jsonl`.
- Cada tick que llega 100 ms tarde o más deja una línea en `$WORK/gui_lag.jsonl` (ver la sección 6). Indica cuándo pasó y cuánto tardó el gc. Cada corrida empieza con una línea `{"run": …}`.
- El hueco entre dos fases también sale como una parada de ~5 s con la pila `bench_gui_lag.py … main`: es la sonda, no cuenta. Los números de cada fase son los de la consola y `lag.jsonl`. Su `ended_at` sirve para ubicar las paradas por fase.

## 5. Qué devolver

`$WORK/lag.jsonl` y `$WORK/gui_lag.jsonl`, más la salida de consola si algo falló.

## 6. Con la app de verdad

La sonda no abre la app. Para medir el lag mientras se usa la app normal (importar, dockear, mirar resultados):

```bash
taskset -c 0-63 env AMDOCK_GUI_LAG=1 amdockvs   # sin taskset: todas las CPU
```

- Cada minuto añade una línea a `~/.amdockvs/logs/gui_lag.jsonl`: `{"at", "ticks", "p50_ms", "p99_ms", "max_ms", "max_at"}` (ticks de 20 ms; `max_at` es la hora del peor). Al cerrar la app escribe el minuto en curso.
- Empieza a medir cuando la ventana ya está abierta, así que el arranque no cuenta. Antes sí contaba: el ~1 s del primer minuto era eso.
- Cada parada de 100 ms o más tiene su propia línea, `{"stall_ms", "at", "gc_ms"}`. `gc_ms` es lo que tardó el recolector de basura dentro de la parada.
- Para saber dónde se fue el tiempo, py-spy, desde fuera. La app no lee pilas de sus propios hilos: probado con faulthandler y con `sys._current_frames`, las dos formas la tumban. Se lanza así:

  ```bash
  AMDOCK_GUI_LAG=1 py-spy record --idle --threads --nonblocking --rate 100 -f speedscope -o $WORK/app.speedscope.json -- amdockvs
  ```

  Devolver ese archivo junto con `gui_lag.jsonl`.
- `~/.amdockvs/logs/freeze_stacks.log` solo recibe pilas si la GUI se queda congelada ≥ 10 s. Debe quedar vacío. **No bajar `AMDOCK_FREEZE_SECONDS`** por debajo de ~1 s: volcar pilas a menudo tumba la app (segfault con 0,2 s).
- Para saber qué motor se usó: `MSFLOW_ENGINE_HOST=inline` vuelve a la fase A; sin nada, fase B (proceso hijo).

Devolver `gui_lag.jsonl` junto con lo de la sonda, diciendo qué se hizo en cada minuto (más o menos).

## 7. Referencia: PC de desarrollo, 16 CPU, 2026-11-11

`dock+import` con la ventana: p99 4,7 ms, máximo 156 ms. Los picos venían de `insert_graph` en el hilo `msflow-delivery`. Detalle en el doc 10 de ms_flow.
