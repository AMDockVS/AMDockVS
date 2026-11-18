# Shard layout baseline — 2026-11-18

Script: `test/benchmarks/bench_shard_layout.py`. Raw: `shard_layout_2026-11-18.jsonl`.
Machine: 16 CPUs, ext4 on NVMe (`/home`, nvme0n1), pyarrow 26.0.0, ms_flow main `edec519`.
Logical shard = 1000 records. Records: real Enamine H100K (SMILES ~70 B, SDF ~2.4 KB), cycled.
Cold = `POSIX_FADV_DONTNEED` on every data file (dentry/inode caches stay warm — no root).
Not measured: NFS/Lustre (none mounted here) — re-run with `--root` on the lab NFS.

Layouts:
- **A** — one Parquet per shard, ms_flow `ShardWriter` as today (fsync file + dir per shard).
- **A0** — same, without the per-file fsyncs; one `os.sync()` at the end.
- **B** — big Parquet files, one row group per logical shard, one fsync per file.

## 1e8 SMILES (100k shards)

| | A (today) | A0 | B 1M rec/file | B 10M rec/file |
|---|---|---|---|---|
| files | 100 000 | 100 000 | 100 | 10 |
| bytes | 2.90 GB | 2.90 GB | 1.85 GB | 1.85 GB |
| durable write | 384 s | 132 s | 65 s | 64 s |
| read all, cold | 54 s | 56 s | 16 s | 17 s |
| task 1 shard, cold | 0.65 ms | 0.61 ms | 2.4 ms | 22 ms |
| task 32 shards, cold | 18.8 ms | 17.8 ms | 11.9 ms | 28 ms |
| rsync / rm | 13.5 / 3.6 s | 13.9 / 3.5 s | 3.6 / 0.38 s | 3.8 / 0.37 s |
| list + stat | 0.43 s | 0.44 s | ~0 | ~0 |
| footer | — | — | 268 KB | 2.7 MB |

1k and 10k shards scale linearly (10k: A write 36.7 s, A0 13.8 s, B 6.5 s).

## 1e7 SDF (10k shards)

| | A | A0 | B 1M rec/file |
|---|---|---|---|
| files | 10 000 | 10 000 | 10 |
| bytes | 2.56 GB | 2.56 GB | 2.46 GB |
| durable write | 111 s | 67 s | 58 s |
| read all, cold | 54.6 s | 54.4 s | 27.0 s |
| task 1 shard, cold | 5.1 ms | 5.9 ms | 10.6 ms |
| task 32 shards, cold | 167 ms | 180 ms | 91 ms |
| rsync / rm | 7.7 / 0.82 s | 7.8 / 0.83 s | 5.8 / 0.47 s |
| footer | — | — | 5.3 MB |

## Footer fix (1000 row groups/file, 200 random single-shard tasks, cold)

| | footer | task 1 cold | task 1, footer cached per worker |
|---|---|---|---|
| SDF, stats on all columns (today's writer) | 5.3 MB | 9.7 ms | 3.8 ms |
| SDF, `write_statistics=["id"]` | 187 KB | 6.9 ms | 3.6 ms |
| SMILES, stats on all | 268 KB | 2.1 ms | 0.33 ms |
| SMILES, `write_statistics=["id"]` | 182 KB | 1.9 ms | 0.31 ms |

## Reading

- File count by itself is not slow on local ext4/NVMe (100k files listed+stat'd in 0.43 s). The cost
  is per file: ~2.5 ms fsync (250 of the 384 s write) + ~1.3 ms of writer work + footer/metadata
  bytes (+57 % disk on SMILES, mostly per-file footer and the `source_indices` JSON).
- B wins on write (5.9x), read-all (2-3.4x), copy (2-4x), rm (8-10x), disk, and 32-shard tasks.
  Its only loss is a single-shard open (footer parse); with stats on `id` only and the footer
  cached per worker it wins that too (0.31 vs 0.65 ms SMILES, 3.6 vs 5.1 ms SDF).
- Footer size grows with **row-group count**, not bytes: 10k row groups/file = 2.7 MB footer,
  22 ms per open. Keep files at ≤ ~1000 row groups, whatever their byte size.
- `record` min/max statistics are dead weight (nobody filters on a SMILES/SDF blob range); they
  are 96 % of the SDF footer.
