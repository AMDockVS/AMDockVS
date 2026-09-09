# Examples

## Import, prepare, and dock

This example mirrors the supported asynchronous workflow. Replace the input
paths and box coordinates with values for your system.

```python
from pathlib import Path

from amdockvs import AMDockVSRuntime

runtime = AMDockVSRuntime()
try:
    runtime.create_or_open_project(
        name="quickstart",
        folder=Path("./quickstart-project"),
    )

    import_jobs = [
        *runtime.loader.load_ligands(["inputs/ligands.sdf"], executor_name="thread"),
        *runtime.loader.load_receptors(["inputs/receptor.pdb"], executor_name="thread"),
    ]
    runtime.watch_jobs(import_jobs)

    ligands = runtime.molecules.select(role="ligand")
    receptors = runtime.molecules.select(role="receptor")
    receptor = receptors.one()

    prep_jobs = [
        runtime.docking.prepare_ligands(ligands=ligands),
        runtime.docking.prepare_receptors(receptors=receptors),
    ]
    runtime.watch_jobs(prep_jobs)

    runtime.docking.set_grid(
        receptor_id=int(receptor.id),
        center=(12.0, 13.0, 10.0),
        size=(20.0, 20.0, 20.0),
    )
    job_id = runtime.docking.run(
        ligands=ligands,
        receptors=receptors,
        vina_cpu=1,
    )
    runtime.watch_job(job_id)

    print(runtime.docking.result_stats())
finally:
    runtime.shutdown()
```

## Notebook-friendly waiting

Several chemistry methods accept `wait=True` when a blocking call is clearer:

```python
status = runtime.chemistry.standardize_ligands(
    ligands=runtime.molecules.select(role="ligand"),
    executor_name="process",
    wait=True,
)
print(status.status)
```

The repository also contains executable starting points in
`examples/runtime_quickstart.py` and `examples/pipeline_demo.py`, plus focused
notebooks under `notebooks/`.
