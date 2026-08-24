# AMDockVS

`AMDockVS` expone un runtime compuesto sobre MolSuite. La ruta oficial es:

```python
from amdock import AMDockVSRuntime
```

La API pública activa está organizada por dominios:

- `runtime.loader`: carga de ligandos y receptores
- `runtime.molecules`: scopes, filtros, sets y acceso al inventario molecular general
- `runtime.complexes`: relaciones receptor-ligando explícitas para redocking y rescoring
- `runtime.chemistry`: herramientas opcionales de preparación química para ligandos y receptores
- `runtime.qsar`: descriptores, actividades, datasets y modelos QSAR
- `runtime.pockets`: instalación y predicción de cavidades con P2Rank
- `runtime.docking`: preparación con Meeko, grillas, docking y consultas de resultados

## Instalación

El flujo de docking con Vina requiere estas dependencias además de RDKit:

- `meeko>=0.7.1`
- `vina>=1.2.7`

Herramientas opcionales de receptor:

- `reduce` para `runtime.chemistry.protonate_receptors(method="reduce")`
- `pdb2pqr` para `runtime.chemistry.protonate_receptors(method="pdb2pqr")`
- `openmm` para `runtime.chemistry.minimize_receptors(...)`

P2Rank 2.5.1 se instala bajo demanda desde **Molecule Tools → Pocket
Detection**. AMDockVS reutiliza primero
`~/Downloads/p2rank_2.5.1.tar.gz`, si existe, y en caso contrario descarga la
distribución oficial verificando su checksum. Java 17 también se instala bajo
demanda en el entorno Conda activo después de comprobar con un `dry-run` que
la transacción no sustituya Python, RDKit, PyMOL, PySide, NumPy, Vina o Meeko.
P2Rank no se distribuye dentro de la aplicación.

La misma operación está disponible en el backend:

```python
runtime.pockets.install_p2rank()
job_id = runtime.pockets.predict(
    receptor_ids=[receptor_id],
    profile="default",
    threads=1,
)
runtime.wait_for_job(job_id)
sites = runtime.pockets.list_predictions(receptor_id=receptor_id)
```

## Flujo mínimo

```python
from pathlib import Path

from amdock import AMDockVSRuntime

runtime = AMDockVSRuntime()
try:
    runtime.create_project(name="demo", folder=Path("./demo_project"))

    load_jobs = [
        *runtime.loader.load_ligands(["./ligands.sdf"], executor_name="thread"),
        *runtime.loader.load_receptors(["./receptor.pdb"], executor_name="thread"),
    ]
    runtime.wait_for_jobs(load_jobs)

    chemistry_job = runtime.chemistry.standardize_ligands(executor_name="thread")
    runtime.wait_for_job(chemistry_job)

    ligand_scope = runtime.molecules.select(role="ligand")
    receptor_scope = runtime.molecules.select(role="receptor", limit=1)

    descriptor_job = runtime.qsar.compute_descriptors(molecule_set=ligand_scope, executor_name="thread")
    runtime.wait_for_job(descriptor_job)

    receptor = next(runtime.molecules.stream(receptor_scope))
    runtime.docking.set_grid(
        receptor_id=int(receptor.id or 0),
        center=(12.0, 13.0, 10.0),
        size=(20.0, 20.0, 20.0),
    )

    prep_jobs = [
        runtime.docking.prepare_ligands(executor_name="thread"),
        runtime.docking.prepare_receptors(executor_name="thread"),
    ]
    runtime.wait_for_jobs(prep_jobs)

    receptor_set = runtime.molecules.create_set(receptor_scope, name="default_receptor_set")
    docking_job = runtime.docking.run(
        ligand_set=ligand_scope,
        receptor_set=receptor_set,
        executor_name="thread",
    )
    runtime.wait_for_job(docking_job)

    results = runtime.docking.list_results()
finally:
    runtime.shutdown()
```

## Semántica async

- `loader.*`, `qsar.compute_descriptors()`, `docking.prepare_*()` y `docking.run()` envían jobs y retornan `job_id` o `list[job_id]`.
- `chemistry.*` también envía jobs por defecto; usa `wait=True` o `runtime.wait_for_job(...)` si quieres bloquear en notebooks.
- La espera es explícita con `runtime.wait_for_job(...)` o `runtime.wait_for_jobs(...)`.
- `runtime.docking.screen(...)` también es async por defecto. Usa `wait=True` solo cuando quieras un helper compuesto bloqueante.

## Notas de diseño

- `stored_path` sigue apuntando al artefacto base importado.
- `chemistry.*` registra una ruta química actual en metadata; los pasos posteriores la consumen si existe.
- La preparación para Vina se guarda sobre `amdock_molecules`, sin una capa separada de trazabilidad por estado.
- La lectura de inventario usa `molsuite.query.project_rows(...)`; no depende del helper privado `DataAccess`.

## Ejemplos

- [examples/pipeline_demo.py](examples/pipeline_demo.py)
- [examples/runtime_quickstart.py](examples/runtime_quickstart.py)
