# AMDockVS

`AMDockVS` exposes a composite runtime on top of MolSuite. The official entry point is:

```python
from amdockvs import AMDockVSRuntime
```

The active public API is organised by domain:

- `runtime.loader`: ligand and receptor loading
- `runtime.molecules`: scopes, filters, sets and access to the general molecular inventory
- `runtime.complexes`: explicit receptor-ligand relations for redocking and rescoring
- `runtime.chemistry`: optional chemical preparation tools for ligands and receptors
- `runtime.qsar`: descriptors, activities, datasets and QSAR models
- `runtime.pockets`: P2Rank installation and cavity prediction
- `runtime.docking`: Meeko preparation, grids, docking and result queries

## Installation

Python >= 3.12. The three MolSuite dependencies are not on PyPI yet, so they are
installed from git first. PyMOL is required and must be installed from
conda-forge because it is not distributed on PyPI:

```bash
conda create -n amdockvs -c conda-forge python=3.12 pymol-open-source
conda activate amdockvs
pip install "ms_flow @ git+https://github.com/MolSuite/ms_flow"
pip install "ms_components @ git+https://github.com/MolSuite/ms_components"
pip install "ms_contactmap @ git+https://github.com/MolSuite/ms_contactmap"
pip install "amdock-vs @ git+https://github.com/AMDockVS/AMDockVS"
```

Start the GUI:

```bash
amdockvs
```

### Optional extras

```bash
pip install "amdock-vs[all]"   # or pick one: bblean, pockets, receptors
```

| Extra | What it adds |
|---|---|
| `bblean` | BitBIRCH clustering (GPL, invoked through its CLI, never imported) |
| `pockets` | the jlink-trimmed JDK that P2Rank runs on |
| `receptors` | OpenMM/PDBFixer receptor rebuilding and minimisation, ProDy fallbacks |

### Non-pip dependencies (optional)

| Tool | What for | Installation |
|---|---|---|
| `autogrid4` | ad4 scoring | `conda install -c conda-forge autogrid` |
| `reduce` | `runtime.chemistry.protonate_receptors(method="reduce")` | `conda install -c conda-forge reduce` |
| `pdb2pqr` | `runtime.chemistry.protonate_receptors(method="pdb2pqr")` | `pip install pdb2pqr` |

P2Rank 2.5.1 is installed on demand from **Molecule Tools → Pocket Detection**.
AMDockVS first reuses `~/Downloads/p2rank_2.5.1.tar.gz` if it exists, and
otherwise downloads the official distribution and verifies its checksum. Java 17
is likewise installed on demand into the active Conda environment, after a
`dry-run` check that the transaction will not replace Python, RDKit, PyMOL,
PySide, NumPy, Vina or Meeko. P2Rank is not redistributed inside the application.

The same operation is available from the backend:

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

## Minimal workflow

```python
from pathlib import Path

from amdockvs import AMDockVSRuntime

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

## Async semantics

- `loader.*`, `qsar.compute_descriptors()`, `docking.prepare_*()` and `docking.run()` submit jobs and return a `job_id` or a `list[job_id]`.
- `chemistry.*` also submits jobs by default; use `wait=True` or `runtime.wait_for_job(...)` to block in notebooks.
- Waiting is explicit, through `runtime.wait_for_job(...)` or `runtime.wait_for_jobs(...)`.
- `runtime.docking.screen(...)` is async by default too. Use `wait=True` only when you want a blocking composite helper.

## Design notes

- `stored_path` keeps pointing at the base imported artifact.
- `chemistry.*` records a current chemical path in metadata; later steps consume it when present.
- Vina preparation is stored on `amdock_molecules`, without a separate per-state traceability layer.
- Inventory reads go through `molsuite.query.project_rows(...)`; they do not depend on the private `DataAccess` helper.

## Examples

- [examples/pipeline_demo.py](examples/pipeline_demo.py)
- [examples/runtime_quickstart.py](examples/runtime_quickstart.py)

## License

MIT — see [LICENSE](LICENSE).
