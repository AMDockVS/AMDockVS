# Prueba de lag de la GUI en el laboratorio (fases A y B, plan 10 de ms_flow)

Esta prueba mide cuánto se retrasa el hilo de la GUI mientras el motor hace docking e importa. En la fase B, ms_flow corre el motor en un proceso hijo (`--engine-host process`, el valor por defecto); `--engine-host inline` lo deja en el proceso de la app, como en la fase A. Hay que correrla en el Threadripper con 16, 32 y 64 workers, en los dos modos.

**Criterio:** con 64 workers, p99 < 50 ms y máximo < 500 ms.

## 1. Instalar las ramas

En el entorno de la app del lab (el que usa `amdockvs`):

```bash
git clone https://github.com/AMDockVS/AMDockVS.git && cd AMDockVS   # o `git fetch` si ya está
git switch feat/engine-child-process   # sonda + fase B
pip install -e . --no-deps
pip install --no-deps "git+https://github.com/MolSuite/ms_flow@feat/engine-child-process"
# Fase 0 del monitor (solo afecta a --window):
pip install --no-deps "git+https://github.com/MolSuite/ms_components@perf/monitor-tasks-diff"
```

Comprobar con `pip show ms_flow` que la ubicación es la de la rama. `python -c "import ms_flow._api_impl.engine_host"` no debe fallar.

## 2. Preparar

- **Un SDF de moléculas pequeñas** (2D vale), por ejemplo `Enamine_H100K.sdf`. La sonda toma de él los ligandos del docking y lo repite hasta `--big-gb` para el import.
- **Un disco con espacio** para `--work`: `--big-gb` más unos 5 GB. Todo se borra al terminar, salvo `--keep`.
- **No hace falta abrir la app ni tener sesión gráfica:** sin `DISPLAY`, corre offscreen.
- **Máquina libre:** cerrar otras cargas pesadas mientras corre.

## 3. Correr

Desde la carpeta del repo, una corrida por número de workers (seis corridas, cada una ~5 min más la preparación):

```bash
LIB=/ruta/a/Enamine_H100K.sdf
WORK=/disco/grande/gui_lag
mkdir -p $WORK
for host in process inline; do
  for cpus in 0-15 0-31 0-63; do
    taskset -c $cpus python test/benchmarks/bench_gui_lag.py --library $LIB --work $WORK \
        --window --big-gb 60 --dump-ms 100 --engine-host $host --out $WORK/lag.jsonl
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
- Con `--dump-ms 100`, cada tick que llega más de 100 ms tarde vuelca las pilas de todos los hilos en `$WORK/gui_lag_stacks.log`. Así se sabe quién tenía el GIL.

## 5. Qué devolver

`$WORK/lag.jsonl` y `$WORK/gui_lag_stacks.log`, más la salida de consola si algo falló.

## 6. Referencia: PC de desarrollo, 16 CPU, 2026-11-11

`dock+import` con la ventana: p99 4,7 ms, máximo 156 ms. Los picos venían de `insert_graph` en el hilo `msflow-delivery`. Detalle en el doc 10 de ms_flow.
