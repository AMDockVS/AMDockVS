M0 baselines (current Parquet shards). Each script uses a throwaway HOME; never opens a real project.
Env: conda MSDD_312; PYTHONPATH=~/.cache/wt/ms_flow/src:$PWD/src:~/PycharmProjects/ms_components/src; QT_QPA_PLATFORM=offscreen.
- bench_import.py: `runtime.loader.shard_ligands` (htpvs import) of N seeded SMILES; wall, parent CPU (SELF user+sys),
  parent VmHWM, worker peak RSS (psutil poll 0.1 s, any one descendant; engine host child counts as a descendant under
  MSFLOW_ENGINE_HOST=process, whose CPU is then NOT in parent CPU). Default host: inline (app default).
- bench_scan.py: N synthetic RESULT_SCHEMA rows via ResultFragmentWriter + record_docked + compact_keys; times a
  `score`-only pq.read_table over key_files (scan_score_s = the gate number); ranking_s (select_hit_refs) is informative.
- bench_gui_lag.py: 20 ms QTimer lag (as install_gui_lag_log) during the import, MSFLOW_ENGINE_HOST=inline|process; max/p99 ms.
Gate commands, each run TWICE (python benchmarks/<x>, from the worktree root):
  bench_import.py --records 100000 --seed 1 --out m0_import_100k_{1,2}.json
  bench_import.py --records 1000000 --seed 1 --out m0_import_1m_{1,2}.json
  bench_scan.py --records 1000000 --seed 1 --out m0_scan_1m_{1,2}.json
  bench_gui_lag.py --records 100000 --mode inline  --out m0_lag_inline_{1,2}.json
  bench_gui_lag.py --records 100000 --mode process --out m0_lag_process_{1,2}.json
Run one at a time, on an idle machine (a running test suite distorts every number).
