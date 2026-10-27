`notebooks/` agrupa notebooks ejecutables de smoke test y exploración para mantener la raíz limpia.

Notebooks actuales:

- `01_runtime_project_smoke.ipynb`
  - valida la superficie de `AMDockVSRuntime`
  - crea un proyecto temporal
  - comprueba que la API pública está segmentada por fachadas

- `02_loader_inventory_smoke.ipynb`
  - importa un ligando y un receptor de ejemplo
  - valida inventario y stats básicos

- `03_scopes_and_snapshots_smoke.ipynb`
  - construye `MoleculeScope`
  - materializa `MoleculeSetRef`

- `04_qsar_descriptors_smoke.ipynb`
  - importa un ligando
  - ejecuta `runtime.qsar.compute_descriptors()`
  - valida salida pública de descriptores

- `05_explicit_sets_qsar_docking_smoke.ipynb`
  - valida `MoleculeScope` como contrato explícito
  - ejecuta `runtime.qsar.compute_descriptors(molecule_set=...)`
  - ejecuta `runtime.docking.run(ligand_set=..., receptor_set=...)`
  - confirma el fallo esperado del resultado cuando el ligando sigue en `SDF`

- `06_qsar_activities_datasets_smoke.ipynb`
  - carga actividades desde CSV
  - crea un `QSARDataset`
  - divide roles `train/test`
  - proyecta el dataset a `MoleculeSetRef`

- `09_vina_vs_100_ligands_smoke.ipynb`
  - importa ligandos via jobs de MolSuite
  - filtra por atomos, peso molecular y un solo fragmento molecular
  - computa descriptores, prepara ligandos/receptor y define caja Vina
  - ejecuta VS con 100 ligandos y `vina_cpu=1` cuando `meeko`/`vina` estan disponibles
  - usa `batch_size=1`, `vina_cpu=1` y `VINA_EXHAUSTIVENESS=1` para probar Vina en chunks paralelos

Reglas:

- los notebooks no son la interfaz oficial del producto
- si un notebook documenta un flujo estable, referenciar siempre `AMDockVSRuntime`
- no escribir fixtures directamente en `project.db` ni depender de `amdock.models` en notebooks de runtime
- cada notebook debe correr top-to-bottom con asserts simples
- no documentar como ejecutable un workflow que todavía no esté implementado

Nota:

- Los notebooks que ejecutan docking real requieren dependencias opcionales (`meeko`, `vina`) disponibles en el entorno activo.
