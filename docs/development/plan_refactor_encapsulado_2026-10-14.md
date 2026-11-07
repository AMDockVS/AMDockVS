# Plan de refactor por encapsulado (2026-10-14)

Objetivo: que lo que cambia junto viva junto. No se corta por número de líneas: un archivo
grande con una sola responsabilidad se queda como está.

Reglas para todas las fases:

- Una fase = una rama + un PR. Ninguna fase cambia comportamiento.
- Commits de "mover" separados de commits de "arreglar". Un `git diff -M` de una fase de mover
  debe mostrar sobre todo renombres.
- Sin shims de compatibilidad: se actualizan todos los imports en el mismo PR y se comprueba con
  `grep -rn "<nombre viejo>" src test` → vacío.
- Guardia: suite completa (`pytest test`) + abrir cada vista tocada en offscreen.

## Diagnóstico

| Archivo | LOC | Commits desde 2026-07 | ¿Problema real? |
|---|---|---|---|
| `docking/api.py` | 1494 | 10 | Sí: `run`, `redock` y `run_shards` repiten ~55 % de sus líneas |
| `qsar/api.py` | 1475 | 8 | Sí: ingestión de actividades (se usa al importar) mezclada con modelado |
| `ui/catalog/receptors.py` | 1060 | 8 | Sí: catálogo + panel de importación + widgets genéricos |
| `ui/tools/molecules/filter.py` | 898 | — | Sí: formularios de importación + código muerto dentro de "filtro" |
| `ui/tools/import_workspace.py` | 616 | — | Asimetría: ligandos aquí, receptores en el catálogo |
| IDs de vistas | — | — | Duplicados y acoplados a módulos Qt |
| `runtime.py` | 679 | 19 | No: es el punto central; métodos cortos (solo `submit_job` > 40 líneas) |
| `ui/main_window.py` | 616 | 17 | No: punto central; cambia por ser el punto de registro |
| `docking/jobs.py` | 806 | 16 | No por ahora: docking + redocking, mismo tema |
| `run_panel.py`, `box.py`, `catalog/results.py` | 950–1070 | — | No: un widget cada uno; partir solo cuando se toque |
| `io/receptor_preview.py`, `engines/vina.py`, `diversity/clustering.py` | 700–970 | — | No: un solo tema, núcleo puro |

## R0 — Código muerto en `ui/tools/molecules/filter.py`

Solo se referencian entre ellos mismos, nadie desde fuera ni en `test/`:

- `prompt_small_molecule_import_prefilter`
- `SmallMoleculeImportPrefilterDialog`
- `SmallMoleculeImportPrefilterForm`
- `ImportQSARFilterForm` (solo lo usa el formulario anterior)

Se borran. Es la fase más barata y deja más limpio el terreno para R2.

## R1 — IDs de vistas en un módulo sin Qt

Nuevo `src/amdockvs/ui/view_ids.py`, solo constantes `str`, sin imports.

Arregla:

- `PREP_STATUS_VIEW_ID` definido en `ui/tools/docking/studio.py:18` y en `preparation_panel.py:36`.
- `DOCKING_VIEW_ID` en `studio.py:17` y en `workflows/steps.py:22` ("kept in sync").
- `"workspace.complexes"` con dos nombres: `RESULTS_PIVOT_VIEW_ID` (`results_pivot.py:32`) y
  `COMPLEXES_VIEW_ID` (`catalog/domain_views.py:6`). Decidir: un nombre, o un alias explícito.
- Las tools importan el módulo Qt entero de `ui/catalog/receptors.py` solo para leer
  `RECEPTOR_VIEW_ID` (`studio.py`, `preparation_panel.py`, `binding_sites/detection.py`).

Los ~20 `*_VIEW_ID` pasan todos a `view_ids.py`; cada módulo los importa de ahí.

## R2 — La importación en un solo paquete

Hoy: `LigandImportDialog` en `ui/tools/import_workspace.py`, `ReceptorImportPanel` en
`ui/catalog/receptors.py`, y los formularios de importación de ligandos en
`ui/tools/molecules/filter.py`. Se usan juntos (pestaña Import) y nunca con el catálogo.

Destino `src/amdockvs/ui/tools/importing/`:

| Archivo | Contenido | Viene de |
|---|---|---|
| `__init__.py` | `open_import_view` | `import_workspace.py` |
| `workspace.py` | `FileDropTable`, `_land_on_view`, `_nudge_monitor`, `open_import_view` | `import_workspace.py` |
| `ligands.py` | `LigandImportDialog` | `import_workspace.py` |
| `ligand_forms.py` | `ImportFilterCriteriaForm`, `ImportPrepareForm`, `ImportActivityForm`, `_sniff_activity_columns`, `finalize_import_prefilter_policy` | `molecules/filter.py` |
| `receptors.py` | `ReceptorImportPanel`, `ReceptorImportDialog`, `_ReceptorScanWorker`, `_as_ref_tuple` | `catalog/receptors.py`, `import_workspace.py` |

Imports a actualizar: `ui/catalog/{ligands,molecules,receptors}.py` (`open_import_view`),
`ui/tools/workflow_panel.py:637,700`, `test/test_project_mode.py` (4 sitios).

Resultado: `ui/catalog/receptors.py` ≈ 200 líneas (`ReceptorWidget`, `_receptor_table_config`,
`register_receptors_workspace`, `import_receptors_from_file`, `_create_receptor_set`);
`molecules/filter.py` ≈ 600, solo filtro.

## R3 — Widgets genéricos a `ui/common/widgets.py`

`_FlowLayout`, `_WrapCombo` y `_ChipsSelect` (hoy `ui/catalog/receptors.py:92-288`) no tienen
nada de receptores. Pasan a `ui/common/widgets.py` como públicos (`FlowLayout`, `WrapCombo`,
`ChipsSelect`). Se hace en el mismo PR que R2 (es el mismo archivo el que se vacía).

Subirlos a `molsuite_components`: solo cuando un segundo paquete los necesite.

## R4 — `docking/api.py`: una sola resolución de argumentos de ejecución

No es mover: es quitar duplicación. Medido: `redock` comparte 58/103 líneas no triviales con
`run`; `run_shards` 53/85. Lo repetido: validar `engine_config`, scopes → filtros por programa,
validación del box, `batch_size`, conjuntos de moléculas, directorio de salida, `run_id`.

Propuesta: un método privado (o una función en `docking/submission.py`) que devuelva un
dataclass con todo eso resuelto; `run`, `redock` y `run_shards` solo añaden lo suyo
(conteo de pares, parámetros del job). El resto de `DockingAPI` (consultas de resultados,
grid, residuos) ya son delegaciones de una línea a `results/repository.py`, `readiness.py`,
`residues.py`: se queda.

Guardia extra: los tests de docking/redocking/shards existentes + comparar los
`DockingJobParams` que salen antes y después para las mismas entradas.

## R5 — `qsar/api.py`: la ingestión de actividades aparte

Las actividades se escriben al importar (o desde el editor), los modelos se entrenan mucho
después. Hoy están en la misma clase.

A `src/amdockvs/qsar/activities.py` (funciones que reciben la sesión/proyecto):
`_to_pchem`, `_UNIT_TO_MOLAR`, `_structural_keys_from_*`, el cuerpo de `load_activities` (134
líneas), `_import_missing_molecules` (74), `load_activity_matrix`. `QSARAPI` conserva los
métodos públicos (`set_activity`, `load_activities`, …) como llamadas cortas a ese módulo; la API
pública no cambia.

A revisar en la misma fase (no mover a ciegas): `_structural_keys_from_file` existe para
moléculas importadas antes de las columnas `inchikey`/`canonical_smiles`. La migración de
`runtime.py` añade las columnas pero no las rellena. O se rellena una vez en la migración y se
borra el fallback, o se deja.

## Fuera del plan

- `runtime.py`, `ui/main_window.py`: muchos commits porque son el punto de registro, no por
  mezclar temas. Partirlos reparte cada cambio entre más archivos.
- `docking/jobs.py`: docking y redocking juntos es coherente. Revisar solo si R4 deja
  el redocking claramente aparte.
- Widgets grandes de un solo tema (`run_panel.py`, `binding_sites/box.py`,
  `catalog/results.py`): se parten cuando haya un cambio que lo pida, no antes.

## Orden y tamaño

| Fase | Riesgo | Tamaño aprox. |
|---|---|---|
| R0 código muerto | Muy bajo | −80 líneas aprox. |
| R1 view_ids | Bajo | ~20 archivos, 1-2 líneas cada uno |
| R2 + R3 importación + widgets | Bajo (solo mover) | ~1500 líneas movidas, 10 imports |
| R4 docking run/redock/shards | Medio (toca lógica) | −150 líneas aprox. |
| R5 actividades QSAR | Bajo-medio | ~400 líneas movidas |
