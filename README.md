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

### Optional extras

```bash
pip install "AMDockVS[all]"   # or pick one: bblean, pockets, receptors
```

| Extra | What it adds |
|---|---|
| `bblean` | BitBIRCH clustering (GPL, invoked through its CLI, never imported) |
| `pockets` | the jlink-trimmed JDK that P2Rank runs on |
| `receptors` | OpenMM/PDBFixer receptor rebuilding and minimisation, ProDy fallbacks |

### External tools

Installed by AMDockVS on demand, from **Settings › External tools** or with
`runtime.tools.install(tool_id)`:

| Tool id | What for |
|---|---|
| `p2rank` | Pocket prediction (needs Java: the `pockets` extra) |
| `qvina` | QuickVina 2 docking |
| `adgpu` | AutoDock-GPU docking |
| `openbabel`, `pkasso` | Ligand protonation |

Found on `PATH` (or at the path given in the settings), not installed by AMDockVS:

| Tool | What for | Installation |
|---|---|---|
| `autogrid4` | Maps for ad4 scoring and AutoDock-GPU | `conda install -c conda-forge autogrid` |
| `gnina` | gnina docking (`$AMDOCK_GNINA`, or Settings › Docking) | gnina release binary |
| `reduce` | `runtime.chemistry.protonate_receptors(method="reduce")` | `conda install -c conda-forge reduce` |
| `pdb2pqr` | `runtime.chemistry.protonate_receptors(method="pdb2pqr")` | `pip install pdb2pqr` |

## Documentation

<https://amdockvs.github.io/AMDockVS/>: the Python API guide, examples, and the
API reference.

## License

MIT — see [LICENSE](LICENSE).
