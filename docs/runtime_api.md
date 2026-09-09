# API guide

Use `AMDockVSRuntime` as the public facade. A runtime owns the active MolSuite
project and its executors, so close it with `shutdown()` when the work is done.

```python
from amdockvs import AMDockVSRuntime
```

## Projects and jobs

```python
runtime = AMDockVSRuntime()
runtime.create_or_open_project(name="demo", folder="./demo-project")

jobs = runtime.loader.load_ligands(["ligands.sdf"], executor_name="thread")
statuses = runtime.watch_jobs(jobs)

for status in statuses.values():
    print(status.job_id, status.status)
```

Long operations submit MolSuite jobs. They return a job identifier, or a list
of identifiers for partitioned imports. `watch_job()` and `watch_jobs()` show
the same tqdm progress in a terminal or notebook; use `follow=False` for a
non-blocking formatted snapshot.

## Query molecules without loading everything

`select()` returns a lazy `Selection[MoleculeRecord]`; its `.scope` is the
serializable `MoleculeScope` used by jobs. A selection can be iterated,
counted, streamed, or saved as a stable set.

```python
ligands = runtime.molecules.select(
    role="ligand",
    excluded=False,
)

print(ligands.count())
for molecule in ligands:
    print(molecule.id, molecule.name)

training_set = runtime.molecules.create_set(ligands, name="training ligands")
```

Pass scopes to downstream APIs when the operation should follow a live query;
pass a `MoleculeSetRef` when reproducibility requires a saved snapshot.

## Chemistry and QSAR

Chemistry operations are optional and composable:

```python
job_id = runtime.chemistry.run_ligand_pipeline(
    ["standardize", "protonate", "generate_3d"],
    ligands=ligands,
    executor_name="process",
)
runtime.wait_for_job(job_id)

descriptor_job = runtime.qsar.compute_descriptors(
    molecule_set=ligands,
    executor_name="process",
)
runtime.wait_for_job(descriptor_job)
```

Activities can be assigned directly or loaded from tabular data. Training and
prediction consume molecule scopes or saved set references:

```python
runtime.qsar.set_activity(
    ligand_id=42,
    endpoint="IC50",
    value=25.0,
    unit="nM",
    source="assay-1",
)

model = runtime.qsar.train(
    molecule_set=training_set,
    endpoint="IC50",
    algorithm="random_forest",
)
predictions = runtime.qsar.predict(model=int(model.id), molecule_set=ligands)
```

Consult the generated reference for the complete signatures because available
model options and return values are defined by the current implementation.

## Binding sites and docking

A docking run needs prepared molecules and either a saved receptor grid or an
explicit box:

```python
receptors = runtime.molecules.select(role="receptor")
receptor = receptors.one()

runtime.binding_sites.save_site(
    molecule_id=int(receptor.id),
    name="active site",
    center=(12.0, 13.0, 10.0),
    size=(20.0, 20.0, 20.0),
)

runtime.docking.set_grid(
    receptor_id=int(receptor.id),
    center=(12.0, 13.0, 10.0),
    size=(20.0, 20.0, 20.0),
)
```

P2Rank is available through the central tools facade:

```python
status = runtime.tools.install("p2rank")

job_id = runtime.binding_sites.predict(receptor_ids=[int(receptor.id)])
runtime.watch_job(job_id)
sites = runtime.binding_sites.select(molecule_id=int(receptor.id), source="p2rank")
```

## Cleanup

Always release executors and project resources:

```python
runtime.shutdown()
```

For scripts, a `try/finally` block is the safest pattern.
