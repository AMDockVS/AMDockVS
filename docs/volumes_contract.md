# Structures as payloads: the contract (A0)

Plan: `ms_flow/benchmarks/refactor_optimization/docs/08-plan-volumenes.md`, part A.
ms_flow guide: `ms_flow/docs/payloads.md` (branch `feat/payload-volumes-v7`).

No molecule, prepared input, pose or reference is a file any more. Its bytes are an
ms_flow payload in the project's volumes; the row keeps an id. Projects made before
this are refused at open (`runtime.on_project_activated`), never migrated (D9).

## Columns

| Table | Column | Series | Kind |
|---|---|---|---|
| `molecules` | `current_payload_id` | `molecules` / `receptors` | the format: `sdf`, `pdb`, `cif`, `mol2`, `smi`... |
| `molecules` | `stored_payload_id` | same | the input as imported |
| `molecule_models` | `payload_id` | same as its molecule | `pdb`, `cif`, `sdf` |
| `molecule_models` | `pae_payload_id` | `aux` | `json` (was `files`) |
| `engines` (`EngineState`) | `prepared_payload_id` | `prepared` | `pdbqt` (D10; `files` keeps only non-byte info) |
| `docking_results` | `pose_payload_id` | `poses` | the pose as the engine gives it, all modes |
| `complexes` | `reference_receptor_payload_id`, `reference_ligand_payload_id` | `references` | as imported |

Every column has `REFERENCES ms_payloads(id)` and an index. `sets.crystal_pose_path`
is gone (unused). Series names live in `amdockvs.core.constants` (`SERIES_*`).

- A molecule with a structure always has `current_payload_id`. `stored_payload_id`
  is what was imported. When the input is the current form, both columns hold the
  **same id** (no second copy, D11). An edit or a Build writes a new payload and
  changes `current_payload_id` only. A sequence-only protein has both NULL.
- A docked pose that becomes a molecule's structure (a promoted hit) is the same id
  in both tables. `release` keeps it while anything references it.
- The `DockingResult.metrics` path keys go away: `selected_pose_path`,
  `selected_pose_pdbqt_path`, `reference_ligand_path`, `reference_receptor_path`.
  The selected mode stays as an index in `metrics`; references come from a join on
  `complexes`.

## Writing

- A task returns rows whose `*_payload_id` value is
  `amdockvs.core.payloads.structure_payload(data, kind, series)`. The sink
  (`table_sink`/`graph_sink`) stores the bytes and writes the id in the same
  transaction. The row builders in `amdockvs.core.rows` take an id or a `Payload`.
- Outside a job (an edit in the UI): `store.put(session, [payload])` in the same
  session that updates the row, then commit.
- `kind` is the format without the dot, lower case. Readers branch on it.

## Reading

- `amdockvs.core.payloads.molecule_payload_id(row)`: current, else stored.
- `payload_store(project)`: the `PayloadStore` of a runtime, workspace, project
  store or a job's `project`.
- `read_structure(project, payload_id) -> (text, kind)`.
- A job source: `ms_flow.payloads.with_payloads(project, rows, {"current_payload_id":
  "ligand"}, kinds=True)`. Each element carries the bytes (`ligand`) and their
  format (`ligand_kind`). Tasks never get a path to a molecule.
- One structure shared by every task (the receptor): `store.materialize(pid,
  output_dir)` once per job; the `File` goes in the elements.
- A program that only reads paths: `with ms_flow.payloads.scratch_file(data,
  ".pdbqt") as path:` inside the task.
- Exports: `store.iter_bytes(ids)`.

## Deleting

Delete the rows, then `store.release(ids_they_held)`. `release` drops only ids no
foreign key references any more. No code deletes molecule, pose or prepared files.

## Still files (D4)

Per-run or regenerable data stays on disk: autogrid maps (a per-receptor cache),
the clustering parquet, QSAR models, `.mshard` shards and `ShardEngineState.files`,
exports, and pose-diagram images (a cache keyed by docking result id, drawn again
on demand). `core/worker_io.py` (`worker_file`, `worker_path_fields`) is for these.

## Deviations from the plan

- D10's `prepared_inputs` table is `EngineState.prepared_payload_id`: `EngineState`
  already is one row per (molecule, role, engine).
- A0-3: no format number; the legacy `molecules.stored_path` column marks an old
  project. The additive `ALTER TABLE` migrations are gone with it.
