`notebooks/` contains executable smoke-test and exploration notebooks, keeping the
repository root uncluttered.

Current notebooks:

- `01_runtime_project_smoke.ipynb`
  - checks the `AMDockVSRuntime` surface
  - creates a temporary project
  - verifies that the public API is organized into domain facades

- `02_loader_inventory_smoke.ipynb`
  - imports a sample ligand and receptor
  - checks the basic inventory and statistics

- `03_scopes_and_snapshots_smoke.ipynb`
  - builds a `MoleculeScope`
  - materializes a `MoleculeSetRef`

- `04_qsar_descriptors_smoke.ipynb`
  - imports a ligand
  - runs `runtime.qsar.compute_descriptors()`
  - checks descriptor output through the public API

- `05_explicit_sets_qsar_docking_smoke.ipynb`
  - checks `MoleculeScope` as an explicit input contract
  - runs `runtime.qsar.compute_descriptors(molecule_set=...)`
  - runs `runtime.docking.run(ligand_set=..., receptor_set=...)`
  - confirms the expected docking failure when the ligand is still in `SDF` format

- `06_qsar_activities_datasets_smoke.ipynb`
  - loads activities from CSV
  - creates a `QSARDataset`
  - splits the train/test roles
  - projects the dataset to a `MoleculeSetRef`

- `09_vina_vs_100_ligands_smoke.ipynb`
  - imports ligands through MolSuite jobs
  - filters by atom count, molecular weight, and single-fragment structures
  - computes descriptors, prepares ligands and receptor, and defines the Vina box
  - runs VS on 100 ligands with `vina_cpu=1` when `meeko` and `vina` are available
  - uses `batch_size=1`, `vina_cpu=1`, and `VINA_EXHAUSTIVENESS=1` to test Vina in parallel chunks

Guidelines:

- Notebooks are not the official product interface.
- When a notebook documents a stable workflow, use `AMDockVSRuntime`.
- Do not write fixtures directly into `project.db` or depend on `amdock.models` in runtime notebooks.
- Each notebook should run from top to bottom with simple assertions.
- Do not describe a workflow as executable until it is implemented.

Note: notebooks that run real docking require optional dependencies (`meeko`, `vina`) in the
active environment.
