# AMDockVS

AMDockVS is a desktop application and Python API for virtual screening:
molecule import and preparation, QSAR, binding sites, docking with several
engines, and result inspection. It runs its jobs through MolSuite.

## Installation

AMDockVS installs into a conda environment ([micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html)
or conda): the compiled scientific stack (RDKit, Vina, Gemmi, NumPy, SciPy, …) comes from
conda-forge, which has builds for Linux and macOS on both Intel and ARM, and AMDockVS itself
from PyPI, together with what is pip-only (PySide6, the `pymol-open-source` 3.2 wheels and the
MolSuite packages):

```bash
curl -LO https://raw.githubusercontent.com/AMDockVS/AMDockVS/main/environment.yml
micromamba create -n amdockvs -f environment.yml   # or: conda env create -f environment.yml
micromamba activate amdockvs
```

Do not `conda install` into that environment afterwards: conda would lay its own builds over
pip's. To change it, edit `environment.yml` and create it again.

`pip install AMDockVS` alone also works on Linux x86-64; elsewhere pip has no Vina wheel and
builds some packages from source.

Start the GUI:

```bash
amdockvs
```

Before opening the interface, AMDockVS checks desktop OpenGL and legacy shader
support in a separate process. It keeps Qt's native platform when compatible.
On Linux, it automatically selects `xcb` only if the native check fails and an
X11 display passes the check. An explicit `QT_QPA_PLATFORM` is respected;
Windows and macOS keep their native backends. Headless runs with
`QT_QPA_PLATFORM=offscreen` or `AMDOCK_DISABLE_PYMOL=1` skip this check.

### Optional extras

```bash
pip install "AMDockVS[all]"   # or pick one: bblean, pockets, prody, qsar
```

| Extra | What it adds |
|---|---|
| `bblean` | BitBIRCH clustering (GPL, invoked through its CLI, never imported) |
| `pockets` | the jlink-trimmed JDK that P2Rank runs on |
| `prody` | ProDy, Meeko's fallback reader for receptor PDBs its own parser rejects. In the conda environment, uncomment `prody` in `environment.yml` instead (Linux x86-64 and Intel macOS only) |
| `qsar` | scikit-learn for QSAR model building and prediction |

### External tools

Installed by AMDockVS on demand, from **Settings › External tools** or with
`runtime.tools.install(tool_id)`:

| Tool id | What for |
|---|---|
| `p2rank` | Pocket prediction (needs Java: the `pockets` extra) |
| `qvina` | QuickVina 2 docking |
| `adgpu` | AutoDock-GPU docking |
| `autogrid` | AutoDock4 maps: ad4 scoring, AutoDock-GPU, AutoDock4Zn (Linux, macOS) |
| `gnina` | gnina docking with CNN rescoring (Linux x86_64, NVIDIA GPU) |
| `unidock` | Uni-Dock GPU-batched docking (Linux, NVIDIA GPU with a CUDA 12.9+ driver) |
| `openbabel`, `pkasso` | Ligand protonation |
| `reduce`, `pdb2pqr` | Receptor protonation |
| `openmm` | Receptor repair (PDBFixer) and minimization (OpenMM) |

## Documentation

<https://amdockvs.github.io/AMDockVS/>: the Python API guide, examples, and the
API reference.

## License

MIT — see [LICENSE](LICENSE).
