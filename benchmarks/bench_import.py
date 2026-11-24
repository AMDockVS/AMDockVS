"""M0 baseline: library import (`htpvs`, shards) of N seeded SMILES.

Measures the import job only (project creation and input generation excluded):
  wall_s            wall time from submit to the job completed
  parent_cpu_s      RUSAGE_SELF user+sys delta over the import (this process = the app/parent;
                    engine host is inline unless MSFLOW_ENGINE_HOST=process)
  parent_peak_rss_mb  VmHWM of this process at the end (a high-water mark: includes the setup
                    before the import, which is tiny next to a 100k+ import)
  worker_peak_rss_mb  largest RSS of any one descendant process, polled every 0.1 s with psutil
  workers_sum_peak_rss_mb  peak of the summed RSS of all descendants alive at once
  children_maxrss_mb  RUSAGE_CHILDREN ru_maxrss (largest reaped child; 0 if workers outlive us)
Usage: python benchmarks/bench_import.py --records N --seed S [--out results.json]
"""
import argparse
import resource
import time
from pathlib import Path

import _common as c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out")
    args = ap.parse_args()

    smi = c.make_smiles(Path(c._HOME) / "library.smi", args.records, args.seed)
    runtime = c.open_project("bench_import")
    sampler = c.ChildSampler()
    try:
        cpu0, t0 = c.cpu_s(), time.perf_counter()
        sampler.start()
        c.run_import(runtime, smi)
        wall = time.perf_counter() - t0
        cpu = c.cpu_s() - cpu0
        sampler.stop()
        from amdockvs.molecules.storage import ShardStore

        n = ShardStore(runtime.workspace.project_db).record_count()
        result = {
            "bench": "import", "records": args.records, "seed": args.seed, "imported_records": n,
            "wall_s": round(wall, 3), "parent_cpu_s": round(cpu, 3),
            "parent_peak_rss_mb": round(c.peak_rss_mb(), 1),
            "worker_peak_rss_mb": round(sampler.peak_one, 1),
            "workers_sum_peak_rss_mb": round(sampler.peak_sum, 1),
            "workers_peak_count": sampler.peak_count,
            "children_maxrss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1),
        }
    finally:
        runtime.shutdown()
    c.emit(result, args.out)


if __name__ == "__main__":
    main()
