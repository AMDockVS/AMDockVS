# Plan: almacenamiento del docking (2026-10-20)

Origen: `docs/benchmarks/vs_classic_2026-10-18/` y la sección "Almacenamiento del docking" de
`docs/TODO.md`. Se hace de una vez, antes de que haya proyectos que migrar: los proyectos creados
antes de esto **no se migran** (ver decisión D1).

Meta medida en el benchmark: de 27 KB por ligando (9 poses) a unos 10-12 KB, sin perder nada que se
use.

## Parte A — ms_flow: varias payloads en un mismo bloque comprimido

Hoy cada payload es un "needle" propio en el volumen. Las 9 poses de un ligando se comprimen por
separado (899 B/pose); juntas en un bloque quedan en 451 B/pose (−50 %).

**Cambio:** varias filas de `ms_payloads` pueden apuntar al mismo needle. Cada fila dice qué trozo
del contenido descomprimido es suyo.

- `ms_payloads` gana dos columnas: `part_offset` y `part_length` (bytes del contenido sin
  comprimir). `NULL` = el needle entero, como hoy. Las apps siguen guardando un id por payload:
  **AMDockVS no cambia ningún lector**, y las interacciones siguen teniendo un `pose_payload_id`
  distinto por pose.
- API: `PayloadGroup(parts=[bytes, ...], kind, series, codec)`. `put()` acepta `Payload` y
  `PayloadGroup` mezclados y devuelve un id por payload y uno por cada parte, en orden. El grupo se
  comprime en el worker al hacer pickle, igual que `Payload`.
- Lectura: `_locate` trae las dos columnas; `_read` descomprime y corta. `get_many`/`iter_bytes`
  agrupan por needle y lo descomprimen una vez (leer las 9 poses = 1 descompresión).
- `check`: el espacio vivo de un volumen se cuenta por needle distinto (`DISTINCT volume_id,
  offset`), no por fila; la verificación del crc se hace una vez por needle.
- `release`: sin cambios (borra filas). El needle queda muerto cuando ya no lo referencia ninguna
  fila; el cálculo de espacio muerto de arriba lo detecta.
- `compact`: copia cada needle distinto una sola vez y mueve todas sus filas con el mismo
  compare-and-set de hoy (`WHERE volume_id = ? AND "offset" = ?`), que ya cubre varias filas.
- `materialize` (a archivo) escribe solo la parte.
- Pruebas: grupo put/get; liberar una parte y compactar (las otras siguen legibles); `check` con
  grupos; filas antiguas con `NULL`; fallo a mitad (puntos de fallo existentes).
- Una rama y un PR en ms_flow; versión nueva de ms_flow, y AMDockVS la exige.

## Parte B — AMDockVS: `docking_results` sin duplicados

Hoy, por pose, `metrics` lleva 1199 B de JSON: 551 del run, 352 del ligando, 247 derivados del
score, 49 de la pose.

**Tabla nueva `docking_runs`** (una fila por `run_id`): engine, backend, run_kind, programa,
protocolo (`protocol_hash` y `protocol_label` como columnas con índice; el resto del protocolo en
JSON), función de puntuación, exhaustiveness, num_modes, caja (JSON), fecha.

**`docking_results` queda así:**

| Columna | Nota |
|---|---|
| id, run_id (índice), receptor_molecule_id, ligand_molecule_id, pose_rank | |
| score, score_type, rmsd_vs_reference | como hoy |
| pose_payload_id, receptor_payload_id | el segundo sale de `metrics` |
| ligand_efficiency, predicted_pki, lle, bei, sei, fit_quality | columnas REAL: se ordenan y filtran sin `JSON_EXTRACT` |
| error | solo en filas fallidas |
| extra (JSON, casi siempre vacío) | lo que es de un engine: CNN de gnina, rmsd lb/ub, rmsd_method |

**Sale de la fila:**
- `predicted_ki_m` (= 10^−pKi), `selected_affinity` (= score del rank 1), `is_selected` (= rank 1),
  `energies` (= las filas hermanas), `generated_at` (= fecha del run), y `lipophilic_efficiency`,
  que es `lle` repetido.
- MW, TPSA, cLogP, HBD/HBA y átomos pesados: ya están en `molecules`.
- Rutas de referencia y de archivos de las filas fallidas.

**Índices:** quitar los duplicados (`index=True` en las FK más `idx_dr_*` iguales) y `idx_dr_engine`.
Queda: run_id, (receptor, ligand, run_id, pose_rank) y pose_payload_id.

**Poses:** el escritor (`docking/tasks.py`) guarda las poses de cada par y run como un
`PayloadGroup`.

**Lugares a cambiar** (según grep): los tres engines (vina, gnina, adgpu) y `docking_metrics`; el
escritor y las interacciones en `docking/tasks.py`; `supersede_results`/`delete_results_where`;
`docking/results/repository.py` y `tables.py` (unos 40 `JSON_EXTRACT`, que pasan a columnas o a un
`JOIN docking_runs`); `docking/repository.py`; `screening/materialize.py`, `shard_jobs.py` y
`shard_tasks.py`; `docking/results/dataset.py` (parquet); y la UI de resultados y detalles. Los
shards guardan su parquet como hoy; solo cambia la forma de la fila que materializan.

## Decisiones que necesito de Mario

- **D1. Proyectos existentes:** no se migran, y sus resultados de docking dejan de leerse (el resto
  del proyecto sí). Alternativa: un lector de compatibilidad para `metrics` antiguo, que es código
  extra que hay que mantener. Propuesta: no migrar.
- **D2. `engine` en la fila:** pasa a `docking_runs` (sale de cada fila y de un índice) o se queda.
  Propuesta: a `docking_runs`.
- **D3. Número de poses por defecto:** no se toca en este cambio (sigue en 9); se decide aparte
  después de medir con gnina.

## Orden

1. Parte A en ms_flow (rama + PR), con pruebas.
2. Parte B en AMDockVS (rama + PR), contra la rama de ms_flow.
3. Repetir el benchmark de 500 ligandos para medir el tamaño final, y comprobar a mano que el visor,
   las interacciones, la exportación, los shards y redocking funcionan.
4. Versión de ms_flow y de AMDockVS.
