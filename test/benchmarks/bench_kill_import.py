"""M13 gate: a library import killed halfway, resumed, and nothing lost or doubled (2026-11-27).

    PYTHONPATH=src:<ms_flow>/src python test/benchmarks/bench_kill_import.py \
        --smi /home/mario/bench_shardfiles/in/lib1m.smi --root /home/mario/bench_gates/kill --kill-after 10

A child process imports the file through `loader.shard_ligands` (compute executor) and is killed
with SIGKILL after `--kill-after` seconds; a second process opens the project, resumes the job and
waits for it. Then: every record once (the records the catalog holds = the lines; no id twice per
scope), every part once in the containers, and the peak RSS of the resuming process tree.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _env(root: Path) -> None:
    for name, var in (("home", "HOME"), ("molsuite", "MOLSUITE_HOME"), ("amdock", "AMDOCK_HOME")):
        os.environ[var] = str(root / name)
    os.environ["AMDOCK_OFFLINE"] = "1"


def child(root: Path, smi: Path) -> None:
    from amdockvs.runtime import AMDockVSRuntime

    _env(root)
    rt = AMDockVSRuntime()
    rt.create_project(name="kill", folder=root / "project")
    jobs = rt.loader.shard_ligands([str(smi)])
    print(json.dumps({"jobs": list(jobs)}), flush=True)
    rt.wait_for_jobs(list(jobs), timeout_s=7200)  # killed before this returns


def _peak(stop: threading.Event, out: list) -> None:
    import psutil

    me = psutil.Process()
    while not stop.is_set():
        total = 0
        for process in [me, *me.children(recursive=True)]:
            try:
                total += process.memory_info().rss
            except psutil.Error:
                pass
        out[0] = max(out[0], total)
        time.sleep(0.25)


def resume(root: Path) -> dict:
    import numpy as np

    from ms_flow.core.data import catalog

    from amdockvs.runtime import AMDockVSRuntime

    _env(root)
    peak, stop = [0], threading.Event()
    threading.Thread(target=_peak, args=(stop, peak), daemon=True).start()
    rt = AMDockVSRuntime()
    try:
        rt.open_project(rt.list_projects()[0].id)  # the one the killed process made
        open_jobs = [job for job in rt.list_jobs() if not str(job.status).startswith(("completed", "failed", "canceled"))]
        took = time.perf_counter()
        resumed = [rt.resume_job(job.job_id) for job in open_jobs]
        states = rt.wait_for_jobs(resumed, timeout_s=7200) if resumed else {}
        took = time.perf_counter() - took
        db = rt.workspace.project_db
        rows = list(catalog.shards(db, whole=True))
        spans = np.asarray([(row["scope"] or 0, row["base_id"]) for row in rows], dtype=np.int64)
        twice = len(spans) - len(np.unique(spans, axis=0)) if len(spans) else 0  # a shard committed twice
        records = sum(int(row["n_records"]) for row in rows)
        return {"interrupted": [str(job.status) for job in open_jobs], "resumed": [str(s.status) for s in states.values()],
                "resume_s": round(took, 1), "records": records, "shards": len(rows), "shards_twice": int(twice),
                "peak_rss_mb": peak[0] >> 20}
    finally:
        stop.set()
        rt.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smi", type=Path, required=True)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--kill-after", type=float, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--role", default="parent")
    a = ap.parse_args()
    if a.role == "child":
        child(a.root, a.smi)
        return
    shutil.rmtree(a.root, ignore_errors=True)
    a.root.mkdir(parents=True)
    lines = sum(1 for _ in open(a.smi, "rb"))
    proc = subprocess.Popen([sys.executable, __file__, "--smi", str(a.smi), "--root", str(a.root),
                             "--kill-after", "0", "--role", "child"], start_new_session=True)
    time.sleep(a.kill_after)
    os.killpg(proc.pid, signal.SIGKILL)  # the app and every process it started
    proc.wait()
    said = {"lines": lines, "killed_after_s": a.kill_after, **resume(a.root)}
    said["lost"] = lines - said["records"]
    print(json.dumps(said), flush=True)
    if a.out:
        with a.out.open("a") as fh:
            fh.write(json.dumps(said) + "\n")


if __name__ == "__main__":
    main()
