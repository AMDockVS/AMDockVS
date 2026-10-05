# AMDockVS

AMDockVS is a chemistry-focused desktop application and Python API for molecular
import, preparation, QSAR, binding-site analysis, docking, and result inspection.
It uses MolSuite for projects, task orchestration, executors, and data flow.

## Install

AMDockVS requires Python 3.12. The MolSuite packages and PyMOL (the
`pymol-open-source` alpha wheels, through `ms_components`) are installed
automatically from PyPI, so no conda environment is needed:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install AMDockVS
```

Start the desktop application with:

```bash
amdockvs
```

## The global selection

Right-click rows in a Ligands or Receptors table and choose **Select** to work on
just those rows. The selection is global: there is one for ligands and one for
receptors, and every table and tool of that role (Build, Diversity, Docking
Studio, including Preview & Run) works on it, whichever one it was made in.

While a selection is active the status bar shows it, for example
`Ligands: 2 selected ✕`, and the ID column of the table is marked as filtered.
Nothing clears it for you: it survives preparing and docking, so a run can be
repeated on the same rows. Click the status-bar entry to drop it and go back to
the whole library.

## Python API

The supported entry point is deliberately small:

```python
from amdockvs import AMDockVSRuntime

runtime = AMDockVSRuntime()
try:
    project = runtime.create_or_open_project(
        name="screening",
        folder="./screening-project",
    )
    print(project)
finally:
    runtime.shutdown()
```

The runtime groups operations by domain:

| Namespace | Responsibility |
| --- | --- |
| `runtime.loader` | Ligand and receptor import |
| `runtime.molecules` | Queries, scopes, filters, and saved sets |
| `runtime.chemistry` | Ligand and receptor preparation |
| `runtime.complexes` | Explicit receptor-ligand relationships |
| `runtime.qsar` | Activities, descriptors, models, and predictions |
| `runtime.binding_sites` | Manual sites and P2Rank predictions |
| `runtime.docking` | Preparation, grids, docking, and results |
| `runtime.diversity` | Clustering and representative selection |

Continue with the [API guide](runtime_api.md), copy a complete
[example](examples.md), or browse the generated [API reference](api_reference.md).

## License

AMDockVS is released under the MIT License.
