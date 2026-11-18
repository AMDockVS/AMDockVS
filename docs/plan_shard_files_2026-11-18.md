# Shards as id ranges of a few big files — plan and state (2026-11-18)

Evidence: `test/benchmarks/results/shard_layout_2026-11-18.md` (layout, up to 1e8),
`shard_pipeline_2026-11-18.md` (inputs + results, 1M–10M, real 14-worker pool) and
`shard_files_app_2026-11-18.md` (the real app import, branch vs baseline).

## Decision

- A **logical shard** stays the unit of work, receipts and the catalog — it stops being a file.
  It is `(path, [base_id, base_id + slot_count))`, one row group per shard inside a big Parquet
  file. Reads prune row groups by the `id` statistics; no row-group index travels anywhere.
- **1000 records per shard** stays the default. Task size is autobatch's job
  (`config.toml: autobatch = 128`, chunks 32), not the shard's. 256 only if a shard must be the
  travelling unit on its own.
- Statistics on `id` (`ligand_id` for results) only. A min/max over SDF/pose text is 96 % of the
  footer and nobody filters on it.
- **Immutable**: a Parquet footer stores byte offsets, so changing one value rewrites the file.
  Everything that "removes" molecules is therefore a sidecar, never an edit (see Exclusions).
- Volumes stay for rows mode (random access to one molecule). Shards are for range scans.
  Two stores, as agreed (`rows-and-shards-two-stores`).

## Built

### 1. Result fragments (PR `perf/result-fragment-row-groups`)

`docking/results/dataset.py` wrote one row group per ligand with statistics on every column: at
1M ligands 12.3 GB instead of 258 MB (48x), a 5.7 MB footer per fragment, top-1000 ranking in
75 s instead of 1.2 s. Now one big row group per fragment, `write_statistics=["ligand_id"]`.
Old fragments stay readable (same schema).

### 2. ms_flow: many shards per file (`feat/shard-id-ranges`)

- `Shard.open(path, ids=(lo, hi))`: only the row groups whose `id` statistics can hold
  `lo <= id < hi` are read; the returned header describes that shard, not the file. `ids=None`
  keeps reading a whole file (every shard written before 2026-11).
- `ShardWriter.end_group()` closes one shard's row group inside the open file.
- Footer cached per process (`pq.read_metadata` once, then `ParquetFile(path, metadata=...)`).

### 3. AMDockVS catalog (`feat/shard-files`)

- `ScreeningShard.base_id` / `slot_count` (NULL = the whole file, legacy and the steps that
  still write a file per shard). Many rows share a `path`. Additive, no migration.
- `shard_ids(row)` is the one way to get a shard's range; the prepared twin's name carries it
  (`molecules.storage.prepared_shard_path`).
- Import writes one rolling file per import batch instead of one file per shard.

**Measured:** a 1M library lands in **1 file** instead of 1002 (SMILES) / 3913 (SDF). Import is
equal or faster (up to 1.8x on SDF with 256-record shards); cold read of everything and
preparation are unchanged.

## Exclusions (built on top)

What a filter, a QSAR cut or a clustering selection removes is **a sidecar of ids, in ms_flow**
— the idea of Iceberg v2 position deletes and Delta deletion vectors, without the dependency.

- `ms_flow.core.data.exclusions`: `exclude(project_db, {scope: ids}) -> artifact id` writes
  `exclusions/<uuid>.parquet` (`scope`, `id`; sorted, atomic rename) and registers it in the
  existing `project_artifacts` (`artifact_kind="exclusion"`, `status="active"`). No new table in
  either repo. `set_exclusion_active(id, bool)` undoes/redoes a cut for free.
- `active_exclusions(project_db)` is the **union of the active cuts**, one sorted array per
  scope, plus `key` (the ids of the cuts in it) as the identity of that state. Cuts accumulate;
  nothing overwrites. Each cut only ever saw what earlier cuts let through, so they are disjoint
  and order-independent.
- `Exclusions.narrow(rows)` turns shard rows into what is left of them: a fully excluded shard
  is dropped, a partial one carries `record_ids`. `Shard.open(..., record_ids=...)` reads only
  those.
- AMDockVS asks and decides, ms_flow holds and resolves (`msflow-app-data-boundary`):
  - `ShardStore.iter_rows()` / `count()` are narrowed by default; `whole=True` is for the steps
    that **transform** rather than select (preparation, chemistry generations), so a library is
    prepared once and undoing a cut needs no re-preparation.
  - `ShardStore.exclude(keys, reason)` is the single entry point, keyed by `shard_key`
    (`import_batch_id`, `record_id`) — the identity that survives original → prepared →
    chemistry outputs, so one cut applies to every derivative.
  - Producers: `qsar.promote_shard_predictions(exclude_rest=True)`,
    `diversity.exclude_non_representatives` (off the run's own sidecar, not `get_run`).
  - `molecules.shard_exclusions()` / `set_shard_exclusion_active()` list and undo them.
- **Docking receipts**: a shard docked while partial writes `selection="excl:<cuts>"`, so
  undoing a cut docks the rest instead of skipping it as already done
  (`docking.results.dataset.shard_selection`, `selection_covers` unchanged).

- **Catalog in ms_flow** (`ms_flow.core.data.catalog`): `ms_shards` + `ms_shard_generations`
  replace `screening_shards` + `shard_generations` (column `scope` = the import batch). Reads come
  pinned to the active generation and narrowed (`whole=True` to skip narrowing). Old projects are
  moved on open (`runtime._move_shard_catalog`, ids kept, `shard_preps` / `screening_shard_runs`
  rebuilt so their foreign key points at `ms_shards`).
- **Undo**: the Filter tool's *Cuts* section (sharded libraries only) lists every cut with a
  checkbox; unticking calls `set_shard_exclusion_active(id, False)`.

## Not done / next

- Activating a generation deletes the grandparent's shards; a `shard_preps` or receipt row that
  still references one makes that delete fail (third rewrite after prep or docking). Needs an
  on_drop hook from ms_flow so the app clears its rows first.
- Filter producing shard cuts itself (today only QSAR and Clustering do).
- Compact exclusion sidecars when they pile up (today the union is read per job feed).
- Phase 2: per-task prepared and result files through one parent rolling writer per stream
  (prepared family, result run/protocol/receptor), HPC per-task files adopted when idle, and
  idle compaction of dead row groups. This replaces the repack plan of the adaptive-task-size
  line: big files make repack mostly moot.
- NFS / Lustre (lab): per-file cost there is larger, which favours big files further; the only
  risk is the cold footer read, cached per worker. Many concurrent readers on one big file over
  NFS is also unmeasured.
