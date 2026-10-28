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

jobs = runtime.loader.load_ligands(["ligands.sdf"])
statuses = runtime.watch_jobs(jobs)

for status in statuses.values():
    print(status.job_id, status.status)
```

Long operations submit MolSuite jobs. They return a job identifier, or a list
of identifiers for partitioned imports. `watch_job()` and `watch_jobs()` show
a tqdm progress bar in a terminal or notebook; `wait_for_job()` and
`wait_for_jobs()` wait without one. A job that reads what another one writes
waits for it, so a pipeline can be submitted at once and waited for at the end.

## Where jobs run

Every job method takes `executor_name`:

- Left out, the job goes to the first active executor that can run it: the
  pool of processes of this machine unless another one was made the default.
- `"thread"` runs it on threads of this process, which suits small jobs.
- The name of an active executor sends it there.

```python
print(runtime.run_destinations())          # [(name, label), ...] active now

job_id = runtime.docking.run(ligands=ligands, receptors=receptors, executor_name="lab-ray")
runtime.resume_job(job_id, "local")        # stop it there and continue it here
```

Hosts and executors are kept by MolSuite and edited from the **Hosts** and
**Executors** pages of Settings, or through `runtime.workspace` (`save_host`, `save_executor`,
`activate`): see the [ms_flow guide](https://molsuite.github.io/ms_flow/guide/#where-jobs-run).
`resume_job()` also continues a job that was interrupted or canceled, without
running again what it had done.

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
)
runtime.wait_for_job(job_id)

descriptor_job = runtime.qsar.compute_descriptors(molecule_set=ligands)
runtime.wait_for_job(descriptor_job)
```

Activities can be assigned directly or loaded from tabular data. Training and
prediction consume molecule scopes or saved set references, and training
needs at least two ligands with the activity:

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

## Binding sites

A binding site is a box on a receptor, drawn by hand or predicted:

```python
receptors = runtime.molecules.select(role="receptor")
receptor = receptors.one()

runtime.binding_sites.save_site(
    molecule_id=int(receptor.id),
    name="active site",
    center=(12.0, 13.0, 10.0),
    size=(20.0, 20.0, 20.0),
)
```

P2Rank predicts them once it is installed through the tools facade:

```python
status = runtime.tools.install("p2rank")

job_id = runtime.binding_sites.predict(receptor_ids=[int(receptor.id)])
runtime.watch_job(job_id)
sites = runtime.binding_sites.select(molecule_id=int(receptor.id), source="p2rank")
```

`runtime.tools.status()` lists every managed tool and whether it is ready.

## Docking

A docking run needs prepared molecules and a grid on each receptor:

```python
runtime.watch_jobs([
    runtime.docking.prepare_ligands(ligands=ligands),
    runtime.docking.prepare_receptors(receptors=receptors),
])
runtime.docking.set_grid(
    receptor_id=int(receptor.id),
    center=(12.0, 13.0, 10.0),
    size=(20.0, 20.0, 20.0),
)

job_id = runtime.docking.run(ligands=ligands, receptors=receptors, exhaustiveness=8)
runtime.watch_job(job_id)
print(runtime.docking.result_stats())
```

`program` chooses the engine: `vina` (the default), `qvina` (QuickVina 2),
`gnina`, `adgpu` (AutoDock-GPU) or `unidock` (Uni-Dock). The options of any engine go in
`engine_config`, which is validated against that engine's model; for engines
other than `vina` the keyword arguments `exhaustiveness`, `num_modes` and
`vina_cpu` are not read.

```python
job_id = runtime.docking.run(
    program="gnina",
    ligands=ligands,
    receptors=receptors,
    engine_config={"exhaustiveness": 8, "num_modes": 9, "scoring_function": "rescore"},
)
```

| Program | `engine_config` keys |
|---|---|
| `vina` | `exhaustiveness`, `num_modes`, `scoring_function` (`vina`, `vinardo`, `ad4`), `vina_cpu`, ... |
| `qvina` | `exhaustiveness`, `num_modes`, `vina_cpu`, `energy_range`, `seed` |
| `gnina` | `exhaustiveness`, `num_modes`, `vina_cpu`, `scoring_function` (`rescore`, `none`, `refinement`, `all`), `seed` |
| `adgpu` | `nrun`, `num_modes`, `parameter_file`, `derived_types`, `seed` |
| `unidock` | `scoring_function` (`vina`, `vinardo`), `search_mode` (`fast`, `balance`, `detail`), `num_modes`, `energy_range`, `seed` |

`runtime.docking.list_programs()` describes them. A pair already docked with
the same protocol is skipped unless `skip_existing=False`.

## Complexes

A complex names a receptor-ligand pair explicitly, for redocking or
rescoring a known pose:

```python
complex_record = runtime.complexes.create(
    receptor_molecule_id=int(receptor.id),
    ligand_molecule_id=42,
    purpose="redocking",
)
print(runtime.complexes.count(purpose="redocking"))
```

## Diversity

Clustering runs once as a job; choosing what represents each cluster is a
separate step that can be repeated on the stored run:

```python
job_id = runtime.diversity.cluster_job(method="bitbirch", threshold=0.65)
runtime.watch_job(job_id)

run = runtime.diversity.list_runs()[0]
runtime.diversity.pick_representatives(run["cluster_run_id"], mode="medoid")
representatives = runtime.diversity.save_centroids_as_set(run["cluster_run_id"], name="diverse")
```

`runtime.diversity.supported_methods()` and `representative_modes()` list the
choices.

## Cleanup

Always release executors and project resources:

```python
runtime.shutdown()
```

For scripts, a `try/finally` block is the safest pattern.
