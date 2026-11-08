# AMDockVS

AMDockVS is a desktop application and Python API for virtual screening:
molecule import and preparation, QSAR, binding sites, docking with several
engines, and result inspection. It runs its jobs through MolSuite.

## Installation

Python >= 3.12. The MolSuite packages and PyMOL (the `pymol-open-source` alpha
wheels, via `ms_components`) are installed automatically from PyPI:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install AMDockVS
```

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
| `prody` | ProDy, Meeko's fallback reader for receptor PDBs its own parser rejects |
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
