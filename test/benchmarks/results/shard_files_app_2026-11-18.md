# App import into shards: big multi-shard Parquet (branch) vs one file per shard (main) — 2026-11-18

Script: `test/benchmarks/bench_shard_files_app.py`. Raw: `shard_files_app_2026-11-18.jsonl`.
Real `loader.shard_ligands` on the compute executor, 1M records (Enamine H100K x10), /home NVMe.
Read = cold, every shard through `ShardAPI.open`. Prep = `prepare_ligand_shards` on 4 shards.

| 1M | shard | files main → branch | MB main → branch | import main → branch | read all main → branch | prep 4 |
|---|---|---|---|---|---|---|
| SMILES | 1000 | 1002 → **1** | 27.1 → 20.8 | 18.6 → 17.5 s | 2.78 → 2.29 s | 28.3 → 26.0 s |
| SMILES | 256 | 3913 → **1** | 31.2 → 22.8 | 27.0 → **18.2 s** | 4.10 → 4.56 s | 6.4 → 6.6 s |
| SDF | 1000 | 1002 → **1** | 255 → 239 | 42.4 → 35.1 s | 9.79 → 9.58 s | 25.5 → 26.0 s |
| SDF | 256 | 3913 → **1** | 290 → 244 | 102.5 → **57.3 s** | 13.8 → 12.9 s | 6.7 → 7.1 s |
