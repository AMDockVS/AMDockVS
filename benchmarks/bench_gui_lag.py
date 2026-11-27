"""M0 baseline: GUI-thread timer lag during an import of N records, inline or process engine host.

Offscreen QApplication; a 20 ms QTimer measures how late each tick comes, exactly as
`install_gui_lag_log` (src/amdockvs/app.py) does (lag = gap between ticks - 20 ms, same
definition, same 20 ms period). That function is not called itself because it only writes a
per-minute summary to <home>/logs/gui_lag.jsonl, too coarse for a short run; this script keeps
every tick. The import is the app's `runtime.loader.shard_ligands`, submitted from the GUI thread
inside the running event loop, and the loop is polled (200 ms QTimer) until the job is terminal.
Reports max / p99 / p50 lag in ms over the ticks from submit to completion, tick count, wall time.
`--mode` sets MSFLOW_ENGINE_HOST (inline | process) before the runtime is created.
`--step prep` (M5): the library is imported first, unmeasured, and the measured job is its
ligand preparation (`runtime.docking.prepare_ligand_shards`), whose parts the parent commits.
`--second N2`: an unrelated import of N2 other records is submitted with it, and the lag is
measured until both end. Every writer delivery is timed (`MSFLOW_SLOW_MS=1`, from slow.log, either
process): `writer_max_ms` per writer and `stalls_100ms` = deliveries of 100 ms or more.
Usage: python benchmarks/bench_gui_lag.py --records N --mode inline|process [--step import|prep]
       [--second N2] [--seed S] [--out f]
"""
import argparse
import os
import time
from pathlib import Path

import _common as c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, required=True)
    ap.add_argument("--mode", choices=("inline", "process"), required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--step", choices=("import", "prep"), default="import")
    ap.add_argument("--second", type=int, default=0)
    ap.add_argument("--out")
    args = ap.parse_args()
    os.environ["MSFLOW_ENGINE_HOST"] = args.mode
    os.environ["MSFLOW_SLOW_MS"] = "1"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    qt_app = QApplication.instance() or QApplication([])
    smi = c.make_smiles(Path(c._HOME) / "library.smi", args.records, args.seed)
    runtime = c.open_project("bench_gui_lag")
    if args.step == "prep":
        c.run_import(runtime, smi)
    other = c.make_smiles(Path(c._HOME) / "other.smi", args.second, args.seed + 1) if args.second else None
    slow_log = Path(os.environ["MOLSUITE_HOME"]) / "logs" / "slow.log"
    slow_log.unlink(missing_ok=True)
    state = {"last": time.monotonic(), "late": [], "jobs": None, "status": None, "t0": None, "t1": None}

    def tick():
        now = time.monotonic()
        if state["jobs"] is not None and state["t1"] is None:
            state["late"].append(max(0.0, (now - state["last"]) * 1000 - 20))
        state["last"] = now

    def poll():
        if state["jobs"] is None:
            state["last"], state["t0"] = time.monotonic(), time.perf_counter()
            state["jobs"] = ([runtime.docking.prepare_ligand_shards(executor_name="compute")]
                             if args.step == "prep" else list(runtime.loader.shard_ligands([smi], executor_name="compute")))
            if other is not None:
                state["jobs"] += list(runtime.loader.shard_ligands([other], executor_name="compute"))
            return
        rows = {r.job_id: r for r in runtime.list_jobs()}
        if all(j in rows and rows[j].status in ("completed", "failed", "cancelled") for j in state["jobs"]):
            state["t1"] = time.perf_counter()
            state["status"] = [rows[j].status for j in state["jobs"]]
            qt_app.quit()

    timer = QTimer(qt_app)
    timer.setInterval(20)
    timer.timeout.connect(tick)
    timer.start()
    poller = QTimer(qt_app)
    poller.setInterval(200)
    poller.timeout.connect(poll)
    poller.start()
    QTimer.singleShot(6 * 3600 * 1000, qt_app.quit)
    qt_app.exec()
    timer.stop()
    poller.stop()
    late = sorted(state["late"])
    writers: dict[str, float] = {}
    stalls = 0
    for line in slow_log.read_text().splitlines() if slow_log.exists() else ():
        if " writer " not in line:
            continue
        name, ms = line.split(" writer ", 1)[1].split(":")[0], float(line.rsplit(": ", 1)[1].split()[0])
        writers[name] = max(writers.get(name, 0.0), ms)
        stalls += ms >= 100
    result = {
        "bench": "gui_lag", "mode": args.mode, "step": args.step, "second": args.second,
        "records": args.records, "seed": args.seed,
        "status": state["status"], "wall_s": round((state["t1"] or time.perf_counter()) - state["t0"], 3),
        "ticks": len(late),
        "max_ms": round(late[-1], 1) if late else None,
        "p99_ms": round(late[int(len(late) * 0.99)], 1) if late else None,
        "p50_ms": round(late[len(late) // 2], 1) if late else None,
        "writer_max_ms": writers, "stalls_100ms": stalls,
    }
    runtime.shutdown()
    c.emit(result, args.out)


if __name__ == "__main__":
    main()
