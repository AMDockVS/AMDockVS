# AMDockVS

AMDockVS is a chemistry-focused desktop application and Python API for molecular
import, preparation, QSAR, binding-site analysis, docking, and result inspection.
It uses MolSuite for projects, task orchestration, executors, and data flow.

## Install

AMDockVS requires Python 3.12. Until the MolSuite packages are published on
PyPI, install them from Git before installing AMDockVS:

```bash
conda create -n amdockvs -c conda-forge python=3.12 pymol-open-source
conda activate amdockvs
pip install "ms_flow @ git+https://github.com/MolSuite/ms_flow"
pip install "ms_components @ git+https://github.com/MolSuite/ms_components"
pip install "ms_contactmap @ git+https://github.com/MolSuite/ms_contactmap"
pip install "amdock-vs @ git+https://github.com/AMDockVS/AMDockVS"
```

Start the desktop application with:

```bash
amdockvs
```

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
