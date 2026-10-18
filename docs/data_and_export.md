# Where your data lives

A project is one folder. Molecules, prepared inputs and docked poses are not separate
files in it: their bytes sit in a few large append-only files, and the database keeps a
reference to each one. A project of 700,000 docked molecules is still a few dozen files,
so it copies, backs up and syncs like a small one.

## The project folder

| Path | What it holds |
|---|---|
| `project.db` | The SQLite database: every molecule, set, binding site, result and interaction, plus the index of the structures. |
| `volumes/` | The structures themselves: imported molecules, receptors, prepared PDBQT, poses, references. Files of up to 1 GB, named `000000001.vol`, `000000002.vol`... |
| `executor.db` | The job journal. An interrupted job resumes from it. |
| `config.toml` | Project settings. |
| `logs/` | The project log. |
| `shards/` | HTPVS ligand libraries (`.mshard`). |
| `docking/` | Result fragments of sharded campaigns, and the docking programs' working files. |
| `models/` | QSAR models. |
| `pockets/` | P2Rank outputs; the viewer draws pocket points from them. |
| `clustering_runs/` | The plot data of each diversity clustering run. |
| `cache/` | Files rebuilt on demand (a structure written out for a viewer, docking inputs, a 2D diagram). Safe to delete. |
| `tmp/` | Job temporaries, removed when the job ends. |

A new project holds only the databases, `config.toml` and `logs/`: every other folder
appears the first time something is written there. Exports go wherever you choose in the
save dialog, which opens in the folder of the previous export.

Copy or move the whole folder. Never copy `project.db` without `volumes/`: the database
only points at the structures.

## Keeping storage tidy

Deleting molecules or results, or re-docking, leaves the old bytes in the volumes until
they are compacted. Click the project name in the status bar to open the project summary.
Its **Structure storage** section shows the size and the share of dead bytes:

- **Check** reads every structure and reports anything missing or damaged.
- **Compact** rewrites the volumes that are mostly dead bytes and frees the space.

When a project opens, AMDockVS runs a fast check in the background and posts a notice to
the bell only if something needs a repair. When the last job ends, it compacts the volumes
that are more than half dead bytes on its own.

A script does not get that automatic step. From Python:

```python
level, title, text = runtime.check_storage()   # fast check: ("info" | "warning" | "error", ...)
runtime.compact_storage_if_due()               # compacts when no job runs; None if nothing was due
```

## Exporting

Since structures are not files, getting them out is an export. Right-click rows in a table:

- **Ligands / Receptors** → **Export structures (ZIP)…**: one file per molecule, in its own
  format (`.sdf`, `.pdb`, `.cif`...), named after the molecule.
- **Results** → **Export poses (SDF)…**: one SDF with the selected poses. Each pose carries
  its ligand, receptor, engine, protocol and score, and its RMSD, ligand efficiency and
  predicted pKi when known, as SDF data fields.
- **Results** → **Export poses (ZIP)…**: one file per pose, as the engine wrote it.

From Python, the same functions the menus use:

```python
from amdockvs.core.payloads import payload_store
from amdockvs.io.export import export_poses_sdf, export_structures

store = payload_store(runtime)
hits = runtime.docking.top_hits(limit=100)
export_poses_sdf(store, hits, "top100.sdf")
export_structures(store, [(hit.ligand_name, hit.pose_payload_id) for hit in hits], "top100.zip")
```

`export_structures` writes a ZIP when the destination ends in `.zip`, otherwise a folder.
Both return how many structures were written.

To read one structure in a script, use `read_structure(runtime, payload_id)` from
`amdockvs.core.payloads`. It returns the text and its format.
