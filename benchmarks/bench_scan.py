"""M0 baseline: one-column scan of N docking results.

Setup (not timed): a project is created in a throwaway HOME; N synthetic result rows
(RESULT_SCHEMA, no pose text, has_pose=True) are written through `ResultFragmentWriter` in
fragments of 256 rows (a library shard), each committed like the campaign does (`record_docked`
through one `PairWriter`: the summary and payload containers of the key + done-set).
Timed, through the app's read path (`key_files` -> summary containers of the key):
  scan_score_s      read only the `score` column of every summary container (`read_rows` columns=)
  scan_score_rows   rows read (must equal N)
  ranking_s         `select_hit_refs(top_n=100)`: the Top-N ranking the app runs (SUMMARY_COLUMNS,
                    status=done filter), for information
Usage: python benchmarks/bench_scan.py --records N --seed S [--out results.json]
"""
import argparse
import random
import time
from pathlib import Path

import _common as c

FRAGMENT_ROWS = 256  # --fragment-rows: one library shard


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out")
    ap.add_argument("--fragment-rows", type=int, default=FRAGMENT_ROWS)
    args = ap.parse_args()

    from ms_flow.core.data.catalog import PairWriter
    from ms_flow.core.data.shard import read_rows

    from amdockvs.docking.results.dataset import (
        ResultFragmentWriter, key_files, result_roles, select_hit_refs, state_key,
    )
    from amdockvs.screening.campaign import record_docked

    runtime = c.open_project("bench_scan")
    try:
        db = runtime.workspace.project_db
        key = state_key(engine="vina", protocol_hash="bench", receptor_id=1)
        rng = random.Random(args.seed)
        folder = Path(c._HOME) / "results" / key
        (folder / "parts").mkdir(parents=True)
        pair = PairWriter(db, folder, generation_id=None, roles=result_roles(key), prefix="bench:")
        t0, written, shard_id = time.perf_counter(), 0, 0
        while written < args.records:
            n = min(args.fragment_rows, args.records - written)
            path = folder / "parts" / f"frag_{shard_id}.arrows"
            with ResultFragmentWriter(path, metadata={}, origin=shard_id + 1) as writer:
                for i in range(n):
                    writer.add({
                        "ligand_id": written + i + 1, "shard_id": shard_id, "docking_run_id": 1,
                        "receptor_id": 1, "score": round(rng.uniform(-14.0, -1.0), 3),
                        "score_type": "affinity", "status": "done", "engine": "vina", "has_pose": True,
                        "smiles": "CCOc1ccccc1C(=O)N", "metrics_json": '{"error": null}',
                        "pose_text": "", "pose_suffix": ".pdbqt",
                    })
            record_docked(db, key=key, shard_id=shard_id + 1, result_path=str(path),
                          part=f"bench:{shard_id + 1}", writer=pair)
            written += n
            shard_id += 1
        pair.close()
        setup_s = time.perf_counter() - t0

        files = key_files(db, [key])
        t0 = time.perf_counter()
        rows = sum(read_rows(f, None, ["score"]).num_rows for f in files)
        scan_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        top = select_hit_refs(db, keys=[key], top_n=100)
        rank_s = time.perf_counter() - t0
        result = {
            "bench": "scan", "records": args.records, "seed": args.seed, "files": len(files),
            "file_mb": round(sum(f.stat().st_size for f in files) / 2**20, 3),
            "scan_score_s": round(scan_s, 4), "scan_score_rows": rows,
            "ranking_s": round(rank_s, 3), "ranking_top": len(top),
            "setup_s": round(setup_s, 2), "peak_rss_mb": round(c.peak_rss_mb(), 1),
        }
    finally:
        runtime.shutdown()
    c.emit(result, args.out)


if __name__ == "__main__":
    main()
