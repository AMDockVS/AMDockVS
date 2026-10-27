# AMDockVS vs. un script clásico (estilo AMDock 1.x) — 2026-10-18

## Pregunta

¿Cuesta más correr un cribado con la API de AMDockVS que con un script propio que haga lo mismo
"a mano"? Se midieron tiempo, CPU, RAM, disco y archivos, y se compararon los resultados.

## Caso de estudio

- **Ligandos:** 500 moléculas de `data/Enamine_D10k/Enamine_D10k.sdf` (una de cada 20, en orden).
- **Receptor:** `data/docking/1iep_receptor.pdbqt` (ya preparado), caja conocida
  centro (15.190, 53.903, 16.917), tamaño 20 × 20 × 20 Å.
- **Química (igual en ambos):** Dimorphite-DL a pH 7,4 → ETKDGv3 (seed 0xF00D) → meeko 0.8.0 →
  QuickVina 2.1 con exhaustiveness 8, 9 modos, `--cpu 1`, seed 0.
- **Máquina:** Ryzen 7 3700X (8 núcleos / 16 hilos), 46 GB RAM, NVMe ext4. Ambas corridas limitadas
  a 14 CPUs (`taskset -c 0-13`), una detrás de otra, nunca a la vez.
- **Software:** `main` de AMDockVS, ms_flow, ms_components, ms_contactmap y ms_env, entorno `MSDD_312`.

## Qué hace cada lado

**Script clásico** (`classic.py`): lee el SDF; con `multiprocessing.Pool(14)`, cada worker
protona y genera el 3D en proceso, escribe un `.sdf`, llama a `mk_prepare_ligand.py` como
subproceso y luego a `qvina2.1` como subproceso, y lee el mejor score del `_out.pdbqt`.

**AMDockVS** (`amdockvs_run.py`): la API pública con la configuración por defecto.
`loader.load_ligands` / `load_receptors` → `chemistry.run_ligand_pipeline(["protonate",
"generate_3d"])` → `docking.prepare_ligands(program="qvina")` → `docking.set_grid` →
`docking.run(program="qvina", engine_config={...})`. Corre con un HOME temporal, así que no
registra nada en `~/.molsuite`.

**Medición** (`measure.py`): cada corrida se lanza en un scope cgroup propio (`systemd-run --user
--scope`) con `taskset`:

| Métrica | Cómo se obtuvo |
|---|---|
| CPU | `cpu.stat` del cgroup: todo el árbol, subprocesos incluidos |
| RAM | Suma del PSS (y del RSS) de todo el árbol, muestreada cada 0,5 s; además `memory.peak` del cgroup |
| Disco | Tamaño real y ocupado de lo que queda en la carpeta de salida |

La comparación de scores está en `compare_scores.py`. Los scores de AMDockVS se leyeron de
`project.db` emparejando por `source_index`.

## Ajustes que necesitó el script clásico

El script ingenuo falló antes de dar resultados válidos. En los dos casos AMDockVS ya trae la
solución por defecto:

1. **Anillos de 7 o más átomos:** meeko, por defecto, los marca como flexibles y escribe los tipos
   `CG0`/`G0`, que QuickVina rechaza (`"CG0" is not a valid AutoDock type`). Se añadió
   `--rigid_macrocycles`.
2. **Sales** (`.Cl`, `.Br`): meeko rechaza moléculas de varios fragmentos. Esto no se corrigió en el
   script, para dejar constancia del fallo, así que esos 2 ligandos (Z3072634657, Z2568340255)
   **no se docaron**. AMDockVS se queda con el fragmento orgánico mayor y sí los docó
   (−8,2 y −7,8 kcal/mol).

## Resultados

| | Script clásico | AMDockVS | Diferencia |
|---|---|---|---|
| Ligandos docados | 498 | **500** | +2 |
| Tiempo total | 1289 s | 1300 s | +0,9 % |
| Tiempo por ligando docado | 2,59 s | 2,60 s | empate |
| CPU total (user + sys) | 17 658 s | **14 827 s** | −16 % |
| CPU por ligando docado | 35,5 s | **29,7 s** | −16 % |
| Uso medio de las 14 CPUs | 97,8 % | 81,4 % | — |
| RAM pico (suma de PSS) | 1832 MB | **1417 MB** | −23 % |
| RAM pico (suma de RSS) | 3960 MB | **2174 MB** | −45 % |
| `memory.peak` del cgroup (incluye page cache) | 1954 MB | **1297 MB** | −34 % |
| Máximo de procesos a la vez | 30 | 28 | — |
| Archivos que deja | 1499 | **15** | −99 % |
| Bytes reales | 14,8 MB | 13,6 MB | −8 % |
| Ocupado en disco (bloques de 4 KB) | 17,3 MB | **13,6 MB** | −21 % |

Tiempos de AMDockVS por fase:

| Fase | Tiempo |
|---|---|
| Arranque del proyecto | 0,1 s |
| Importación | 2,0 s |
| Protonación + 3D | 4,0 s |
| meeko | 2,9 s |
| Docking | 1290 s |

**Los resultados son idénticos:** los 498 ligandos comunes tienen exactamente el mismo mejor score
en los dos lados (Pearson = Spearman = 1,0).

**No se pudo medir la escritura a disco.** El cgroup de usuario no tiene el controlador `io`, y el
delta de `/proc/diskstats` mide todo el dispositivo: dio ~330 MB en las dos corridas, casi todo de
otros procesos. Se descartó.

## Por qué salen así

- **El tiempo empata porque manda QuickVina:** ~36 s por ligando, el 99 % del total. Dos efectos de
  signo contrario se compensan:
  - El script arranca un Python con meeko **por cada ligando**: 1,35 s de pared, 3,5 s de CPU y
    258 MB por llamada. Esos ~1750 s de CPU explican la mayor parte de su exceso de CPU y los picos
    de RAM.
  - AMDockVS pierde ~45 s al final porque agrupa 4 ligandos por chunk de docking. En los últimos
    ~100 s quedan CPUs paradas, pero eso solo explica ~4 puntos del uso medio.
- **El uso medio (98 % vs 81 %) no mide eficiencia.** En las dos corridas QuickVina usa ~0,85 s de
  CPU por segundo de pared: 14 827 s de CPU entre ~17 400 s de slots ocupados en AMDockVS. El 98 %
  del script no viene de docar mejor: son ~3000 s de CPU extra (arrancar Python y meeko por
  ligando) que ocupan las CPUs al mismo tiempo que el docking.
- **Tamaño del batch de docking.** Se simuló el reparto con las 498 duraciones reales:

  | Ligandos por chunk | Tiempo total simulado |
  |---|---|
  | 1 | 1299 s |
  | 2 | 1317 s |
  | 4 (por defecto) | 1345 s |
  | 8 | 1437 s |
  | Ideal (sin cola) | 1275 s |

  Con 500 ligandos, pasar de 4 a 1 ahorraría ~3,5 %; con 5000, menos del 0,5 %. No justifica
  cambiarlo, salvo quizás usar chunks más pequeños cuando hay pocos pares por CPU.
- **RAM.** QuickVina corre desde hilos del proceso principal, sin un worker de Python por cada run,
  y meeko se importa una sola vez. El script tiene 14 workers con RDKit, más hasta 14 meeko
  arrancando a la vez.

## Disco a escala (proyección lineal, sin correrlo)

**Por ligando con 9 poses:**
- **Clásico:** 34,6 KB ocupados en 3 archivos (`.sdf` 5,3 + `.pdbqt` 4,1 + `_out.pdbqt` 25,1 KB).
- **AMDockVS:** 27,1 KB, repartidos así:

| Concepto | KB por ligando |
|---|---|
| Tabla `docking_results` | 12,2 |
| Poses SDF comprimidas (848 B cada una; 3,7 KB sin comprimir) | 7,5 |
| Índices | 2,1 |
| Estructuras de entrada, 3D y PDBQT | 2,7 |
| Filas de moléculas y registro de payloads | 1,0 |

| Ligandos | Clásico | AMDockVS (modo filas, tal como está) |
|---|---|---|
| 1 M | 35 GB en **3 M archivos** | 27 GB en decenas de archivos |
| 10 M | 346 GB en **30 M archivos** | 271 GB (~140 GB en `project.db`) |

- **El límite real del script son los archivos, no los bytes.** Este disco tiene 15 M inodos, así
  que con 10 M ligandos el script falla aunque sobre espacio. En HPC las cuotas suelen rondar
  1 M archivos por usuario.
- **AMDockVS desperdicia espacio.** Casi la mitad de su tamaño es la columna `metrics` de
  `docking_results`: ~1,2 KB de JSON **por pose**. Repite en cada fila datos del run (caja,
  protocolo, engine, `run_id`) y del ligando (las 9 energías, MW, TPSA, cLogP, HBD/HBA, eficiencias).
  Guardados una sola vez, la tabla bajaría a ~2 KB por ligando y el total a ~17 KB por ligando
  (~17 GB por millón). Esto encaja con lo que ya se sabe de AMDockVS: tablas sin uso y propiedades
  repetidas en 3 o 4 sitios, pendientes de depurar.
- **Modo con shards** (pensado para millones): `docking.run` exige `hit_threshold`/`hit_cap` y solo
  guarda las poses de los hits. Estimación sin medir: ~27-37 GB para 10 M ligandos más los hits,
  según cuántas versiones de los shards se conserven.

## Prueba aparte: guardar solo coordenadas (2026-10-19)

`coords_test.py`, fuera de AMDockVS, sobre las 4446 poses del benchmark. Las 9 poses de un ligando
tienen siempre la misma topología, en el mismo orden de átomos (0 diferencias). Las variantes
guardan **un payload por ligando** con zstd-19:

| Variante | Por ligando | Por pose | vs hoy |
|---|---|---|---|
| Hoy: un SDF por pose | 7991 B | 899 B | 100 % |
| Los 9 SDF del ligando en un solo payload | 4009 B | 451 B | 50 % |
| Topología + xyz int32 a 1e-4 Å (sin pérdida respecto al SDF) | 3896 B | 438 B | 49 % |
| Topología + xyz int16 a 1e-3 Å desde el centro de la caja | 2622 B | 295 B | 33 % |
| Topología + xyz float16 absolutas (error máx. 0,031 Å) | 2260 B | 254 B | 28 % |
| Topología + xyz float16 desde el centro de la caja (error máx. 0,004 Å) | 2541 B | 286 B | 32 % |
| Lo mismo, solo átomos pesados | 1592 B | 179 B | 20 % |
| Lo mismo, sin topología (se toma de la estructura de entrada) | 1305 B | 147 B | 16 % |

- **Agrupar por ligando da la mitad del ahorro**, porque zstd ya elimina la topología repetida
  dentro de un mismo payload.
- **Bajar la precisión a 1e-3 Å** (la de un PDBQT) y usar int16 desde el centro de la caja da otro
  tercio.
- **int16 frente a float16:** el error de int16 es fijo, 0,0005 Å en toda la caja. El de float16 crece
  con el valor de la coordenada: 0,031 Å aquí, y ~0,06 Å si el receptor está a más de 64 Å del
  origen de su PDB. Depende de dónde cae la proteína.
- **Quitar los H** reduce casi a la mitad lo que queda. Los H apolares se regeneran, pero la
  orientación de los H polares se pierde (sin medir: guardar pesados + H polares).
- **"Sin topología"** supone que el orden de átomos de la pose coincide con el de la estructura de
  entrada guardada. No está comprobado.
- **A 33 M ligandos con 9 poses**, solo las poses: hoy ~264 GB, agrupadas ~132 GB, int16 con H
  ~87 GB, solo pesados ~53 GB. Con top 3 poses, aproximadamente un tercio de cada cifra.

**Por tipo de número** (`dtype_test.py`, solo las coordenadas, 200 214 posiciones de átomo; el átomo
más alejado está a 11,2 Å del centro de la caja):

| Tipo | Bytes por átomo sin comprimir | Con zstd-19 | Error máximo |
|---|---|---|---|
| Texto SDF | 30 | — | 0 |
| float32 | 12 | 9,8 | ~0 |
| int32 en milésimas de Å | 12 | 7,4 | 0,0005 Å |
| int16 en milésimas desde el centro | 6 | 5,8 | 0,0005 Å |
| float16 desde el centro | 6 | 5,6 | 0,004 Å |
| float16 absoluto | 6 | 4,9 | 0,031 Å |

int16 cabe si ninguna coordenada se aleja más de 32,767 Å del centro. Se decide por bloque con un
`max()` sobre los datos y, si no cabe, se usa int32 (un byte del bloque dice cuál es).

## Conclusión

AMDockVS es competitivo:
- **Tiempo:** igual (el tiempo lo pone el motor de docking).
- **Recursos:** −16 % de CPU, −23 % de RAM, 99 % menos archivos y menos disco ocupado.
- **Resultados:** idénticos, y sin fallar con sales ni con anillos de 7 o más átomos.

Lo que de verdad lo distingue no se ve en esta tabla: reanudar tras un fallo, saltar los pares ya
docados, cancelar matando los binarios, resultados consultables con la química de la pose,
interacciones en la misma pasada y escalar a shards u otros hosts sin reescribir nada. El script
solo conserva su ventaja para un cálculo único, en una máquina donde no se quiere instalar nada.

## Pendiente (anotado, sin implementar)

- Quitar de `docking_results.metrics` los datos del run y del ligando que se repiten por pose, y en
  general depurar tablas sin uso y propiedades duplicadas.
- Usar chunks de docking más pequeños cuando hay pocos pares por CPU (~3,5 % con 500 ligandos).
- Una advertencia de API: `docking.top_hits(limit=)` cuenta **poses**, no ligandos. Para un resumen
  por ligando está `ligand_summaries`.

## Cómo reproducirlo

```bash
source /home/mario/programs/miniconda3/etc/profile.d/conda.sh && conda activate MSDD_312
export PYTHONPATH=$PWD/src:$PWD/../ms_flow/src:$PWD/../ms_components/src:$PWD/../ms_contactmap/src:$PWD/../ms_env/src
# copiar estos scripts junto a ligs500.sdf y 1iep_receptor.pdbqt (rutas en measure.py: /tmp/bench_vs)
python measure.py classic classic.py
python measure.py amdock amdockvs_run.py
python compare_scores.py   # lee los scores de AMDockVS directamente de project.db
```
