# Shard pipeline on big Parquet files — 2026-11-18

Script: `test/benchmarks/bench_shard_pipeline.py`. Raw: `shard_pipeline_2026-11-18.jsonl`.
Same machine as `shard_layout_2026-11-18.md` (16 CPUs, ext4 on NVMe `/home`). 14 workers.
Plan built on this: `docs/plan_shard_files_2026-11-18.md`.

## Inputs: can a shard be cut? (task = run of row groups, footer cached per worker, cold)

**SDF (~2.4 KB/record — stands in for PDBQT)**

| 10M SDF | A (today, 1000/file) | B128 | B256 | B1000 |
|---|---|---|---|---|
| files | 10 000 | 79 | 40 | 10 |
| GB | 2.56 | 2.47 | 2.40 | 2.36 |
| write, durable | 87.4 s | 58.9 s | 53.5 s | 55.6 s |
| read all | 59.6 s | 34.8 s | 35.2 s | 31.8 s |
| task 128 records | 5.95 ms | **1.49 ms** | 2.15 ms | 4.98 ms |
| task 1000 | 10.8 ms | **5.4 ms** | 6.2 ms | 8.4 ms |
| task 32 000 | 200 ms | 115 ms | 115 ms | 116 ms |

**SMILES (~70 B/record)**

| 10M SMILES | A | B128 | B256 | B1000 |
|---|---|---|---|---|
| files | 10 000 | 79 | 40 | 10 |
| MB | 280 | 219 | 200 | 183 |
| write, durable | 38.2 s | 13.7 s | 9.6 s | 6.4 s |
| read all | 7.2 s | 3.5 s | 3.1 s | 2.4 s |
| task 128 | 0.87 ms | 0.39 ms | 0.40 ms | 0.68 ms |
| task 1000 | 1.50 ms | 0.80 ms | 0.71 ms | 0.94 ms |
| task 32 000 | 24.4 ms | 12.3 ms | 10.0 ms | 8.5 ms |

1M behaves the same (SDF: A write 9.4 s / read 6.1 s; B256 5.6 s / 3.2 s).
Footer: 170-183 KB at 1000 row groups/file, 1.6-1.9 ms to open cold, once per worker.

Reading: every B beats today on every column. Smaller row groups cost ~nothing on SDF
(the payload dominates) and a little on SMILES. **B256** is the balance: a 128-record task
reads 2.8x faster than today, a whole-shard task 1.7x, write 1.6-4x, 40x fewer files.

## Results: docking output (14 workers, synthetic SDF-size poses)

| 1M results | R0 (today) | R1, 1000/task | R1, 128/task | R2, 1000/task | R2, 128/task |
|---|---|---|---|---|---|
| files | 1 000 | 1 000 | 7 813 | **1** | 8 |
| bytes | **12.3 GB** | 258 MB | 289 MB | 257 MB | 280 MB |
| footer | 5.7 MB | 2 KB | 2 KB | 784 KB | 764 KB |
| write | 40.9 s | 3.4 s | 3.8 s | 7.9 s | 9.8 s |
| rank top-1000 | 75.2 s | 1.17 s | 6.19 s | **0.13 s** | 0.66 s |
| fetch 1000 poses | 22.0 s | 3.6 s | 2.3 s | 2.5 s | **0.71 s** |

| 10M results | R1, 1000 | R1, 128 | R2, 1000 | R2, 128 | adopt (R1-128 → rolling) |
|---|---|---|---|---|---|
| files | 10 000 | 78 125 | 10 | 79 | 78 125 → 79 |
| GB | 2.58 | 2.89 | 2.57 | 2.80 | 2.80 |
| write | 43 s | 44 s | 89 s | 542 s | 170 s |
| rank top-1000 | 9.4 s | 58 s | **1.4 s** | 6.6 s | — |
| fetch 1000 poses | 7.8 s | 15.6 s | 3.2 s | **1.5 s** | — |

Reading:
- **R0 is the real problem today**: one row group per ligand + statistics on every column →
  48x the bytes, 5.7 MB footers, ranking 75 s at 1M (vs 0.13-1.2 s). Fixing it needs none of
  the shard change (plan §1).
- R2 (parent rolling file) ranks 7-45x faster than per-task files, because ranking pays per
  file. Its cost is that one parent thread compresses everything: ~30 MB/s sustained (1M and
  10M at 1000/task). At 10M with 128-ligand tasks the bench flooded the parent with 78k
  pending tables (`pool.map`, no backpressure) and it fell to ~5 MB/s. Real docking produces
  ~0.04 MB/s (15 lig/s x 2.5 KB) — 1000x headroom — but the sink needs a bounded queue.
- Adoption (HPC files → rolling) costs ~2.2 ms/file (re-encode). Fine when idle; optional.

## Not measured

NFS/Lustre; concurrent readers on one big file; the import's RDKit work (this is storage only).

## 1M ligands written: SQLite / SQLite+volumes / SQLite+shards (storage only)

`bench_storage_formats.py --stages write1m --workdir /home/mario/bench_storage`, raw in
`storage_formats_2026-11-18.jsonl`. Enamine H100K cycled to 1M, records already parsed with
descriptors and payloads zstd-packed (the workers do that in a real import); durable (WAL
checkpoint + `os.sync`). `molecules` with today's schema and indexes, one transaction per 10k.

| 1M SDF | write | CPU | files | on disk | project.db |
|---|---|---|---|---|---|
| sqlite_blob — molecules + SDF blobs in project.db | 38.2 s | 37.7 s | 5 | 817 MB | 817 MB |
| sqlite_volumes — molecules + volumes (rows import today) | 38.5 s | 38.1 s | 6 | 835 MB | 256 MB |
| shards_today — screening_shards + 1000 Parquet (HTP today) | 10.4 s | 8.7 s | 1005 | 256 MB | 0.8 MB |
| shards_big — screening_shards + 1 Parquet, rg = shard | **5.4 s** | 5.4 s | 6 | **239 MB** | 0.8 MB |
| sqlite_shards_big — molecules + shards_big | 37.3 s | 37.1 s | 6 | 432 MB | 194 MB |

Reading:
- ~32 of the ~38 s is the `molecules` table (rows + secondary indexes), CPU-bound in
  SQLAlchemy/SQLite — not the disk. Where the structure goes (blob, volume, Parquet) is ~1-5 s.
- Per-record zstd (blob, volume) = ~580 B/molecule; Parquet's column zstd = ~240 B (2.4x less).
- vs 2026-10-22 (100k, old disk at 96 %, extrapolated x10): rows+volumes 57 → 38 s,
  shards 1000/file 24 → 10.4 s, descriptor rows with indexes 51 → ~32 s.

## Container: Parquet vs SQLite files of blobs (1M SDF, storage only)

`bench_sqlite_container.py`, raw in `sqlite_container_2026-11-18.jsonl`. Shard = 256 records.
SQLite = `(id INTEGER PRIMARY KEY, data BLOB)`, per-record zstd, WAL + synchronous=FULL.
Reads cold, 50 random tasks, decompressed; SQLite opened `immutable=1`.

| 1M SDF | write, durable | durable every | MB | task 256 | task 1000 | task 32 000 | read all |
|---|---|---|---|---|---|---|---|
| Parquet, rg 256, 1000 rg/file | **6.1 s** | file (256k records) | **240** | 1.92 ms | 5.36 ms | 111 ms | 3.6 s |
| SQLite, commit per shard | 17.8 s | shard (256) | 616 | 1.63 ms | 5.46 ms | 158 ms | 5.0 s |
| SQLite + zstd dictionary (10k-record trained) | 11.7 s | shard (256) | 306 | **0.96 ms** | **2.98 ms** | **83 ms** | **2.7 s** |
| SQLite, commit per 10k | 13.9 s | 10k | 616 | 1.62 ms | 5.44 ms | 159 ms | 5.1 s |

Reading: with a dictionary SQLite is 1.27x Parquet's size and reads ~1.3-2x faster by id
range; durable per commit and readable while written. Not measured: ranking a score column
next to pose blobs (Parquet reads only that column; SQLite walks the rows), NFS.
