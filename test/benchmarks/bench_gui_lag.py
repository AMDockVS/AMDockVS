"""How late the GUI thread runs while the engine docks and imports, in the app's own process.

    taskset -c 0-7 python test/benchmarks/bench_gui_lag.py --work /big/disk/dir --ligands 3000

Plan 10 of ms_flow, phase A: a 20 ms QTimer in the GUI thread records how late each tick fires
(p50 / p99 / max) in three phases of `--seconds` each:

1. `idle`: the app with a project open, nothing running;
2. `dock`: a Vina docking of short chunks (exhaustiveness 1, `--batch` pairs per task);
3. `dock+import`: the same docking plus the import of a big SDF read cold from disk.

For each phase it also prints the docking chunks settled per second, which says whether the
import's feed slows the dispatch down. The engine sizes itself from the CPUs this process may use,
so `taskset` sets the number of workers. Criterion of the plan: with 64 workers, p99 < 50 ms and
max < 500 ms. `--dump-ms 100` says which thread held the GIL in a late tick. `--window` also opens the main window (offscreen without a display), so its
monitor and tables refresh in the GUI thread as in the app. `--engine-host inline` keeps the
engine in this process, as before phase B.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[2] / "data" / "Enamine_H100K" / "Enamine_H100K.sdf"
RECEPTOR = Path(__file__).resolve().parents[2] / "data" / "docking" / "1iep_receptorH.pdb"
CENTER, BOX = (15.190, 53.903, 16.917), (22.0, 22.0, 22.0)


def first_records(sdf: Path, n: int, dest: Path) -> Path:
    with open(sdf, "rb") as source, open(dest, "wb") as out:
        for line in source:
            out.write(line)
            if line.startswith(b"$$$$"):
                n -= 1
                if n == 0:
                    break
    return dest


def big_sdf(library: Path, dest: Path, gb: float) -> Path:
    """`library` repeated up to `gb`, then dropped from the page cache so the import reads the disk."""
    chunk = library.read_bytes()
    with open(dest, "wb") as out:
        while out.tell() < gb * 1e9:
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(dest, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    return dest


def lag_stats(lags: list[float]) -> dict:
    if not lags:
        return {"ticks": 0}
    ordered = sorted(lags)
    return {"ticks": len(lags), "p50_ms": round(statistics.median(ordered), 1),
            "p99_ms": round(ordered[int(0.99 * (len(ordered) - 1))], 1), "max_ms": round(ordered[-1], 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, help="where the project and the big SDF go (needs ~gb+5 GB)")
    ap.add_argument("--library", type=Path, default=LIBRARY, help="SDF the ligands and the big import come from")
    ap.add_argument("--ligands", type=int, default=3000)
    ap.add_argument("--batch", type=int, default=1, help="docking pairs per task")
    ap.add_argument("--big-gb", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=60.0, help="length of each phase")
    ap.add_argument("--switch-ms", type=float, default=1.0, help="sys.setswitchinterval, as the app sets it")
    ap.add_argument("--window", action="store_true", help="also open the main window")
    ap.add_argument("--dump-ms", type=float, default=0,
                    help="dump every thread's stack to gui_lag_stacks.log when a tick is this late (0: off)")
    ap.add_argument("--engine-host", choices=("process", "inline"), default="process",
                    help="where ms_flow runs the engine: a child process (phase B) or this one")
    ap.add_argument("--keep", action="store_true", help="keep the work folder")
    ap.add_argument("--out", type=Path, help="append the JSON report here")
    args = ap.parse_args()
    if not args.library.is_file():
        ap.error(f"no SDF at {args.library}: pass --library (any SDF of small molecules, 2D is fine)")

    work = Path(tempfile.mkdtemp(prefix="gui_lag_", dir=args.work))
    os.environ["HOME"] = str(work / "home")  # the project registers here, not in the real ~/.molsuite
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    os.environ["MSFLOW_ENGINE_HOST"] = args.engine_host
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.setswitchinterval(args.switch_ms / 1000)

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from amdockvs import AMDockVSRuntime
    from amdockvs.app import use_en_us_numbers

    qt_app = QApplication.instance() or QApplication(["bench_gui_lag"])
    use_en_us_numbers()
    cpus = len(os.sched_getaffinity(0))
    report: dict = {"cpus": cpus, "ligands": args.ligands, "batch": args.batch, "big_gb": args.big_gb,
                    "switch_ms": args.switch_ms, "window": args.window, "engine_host": args.engine_host,
                    "phases": {}}
    rt = AMDockVSRuntime()
    window = None
    try:
        t0 = time.monotonic()
        rt.create_project(name="gui_lag", folder=work / "project")
        ligands = first_records(args.library, args.ligands, work / "ligands.sdf")
        rt.wait_for_jobs([*rt.loader.load_receptors([RECEPTOR]), *rt.loader.load_ligands([ligands])])
        rt.wait_for_jobs([rt.chemistry.generate_3d_ligands()])
        rt.wait_for_jobs([rt.docking.prepare_receptors()])
        rt.wait_for_jobs([rt.docking.prepare_ligands()])
        (receptor,) = rt.molecules.stream(rt.molecules.select(role="receptor"))
        rt.docking.set_grid(receptor_id=int(receptor.id), center=CENTER, size=BOX)
        receptor_set = rt.molecules.create_set(rt.molecules.select(role="receptor"), name="lag", kind="snapshot")
        big = big_sdf(args.library, work / "big.sdf", args.big_gb)
        report["setup_s"] = round(time.monotonic() - t0, 1)
        print(f"setup {report['setup_s']} s on {cpus} CPUs", flush=True)

        if args.window:
            from amdockvs.ui.main_window import AMDockVSMainWindow

            window = AMDockVSMainWindow(runtime=rt)
            window.show()

        lags: list[float] = []
        at: list[float] = []  # when each tick fired, from the start of its phase
        last = [0.0, 0.0]
        timer = QTimer()
        timer.setInterval(20)
        timer.setTimerType(Qt.TimerType.PreciseTimer)

        stacks = None
        if args.dump_ms:
            import faulthandler

            stacks = open((args.work or Path.cwd()) / "gui_lag_stacks.log", "a", buffering=1)  # noqa: SIM115

        def tick() -> None:
            if stacks is not None:  # fires from a C thread, so it dumps while another thread holds the GIL
                faulthandler.dump_traceback_later((20 + args.dump_ms) / 1000, file=stacks, exit=False)
            now = time.perf_counter()
            lags.append(max(0.0, (now - last[0]) * 1000 - 20))
            at.append(now - last[1])
            last[0] = now

        timer.timeout.connect(tick)
        engine = rt.workspace.engine
        engine_ids = rt._engine_ids()

        def done(job_id: str | None) -> int:
            if job_id is None:
                return 0
            snapshot = engine.status(engine_ids[job_id])
            return snapshot.chunks_done + snapshot.chunks_failed

        def phase(name: str, dock: str | None, load: str | None = None) -> None:
            lags.clear()
            at.clear()
            before, start = done(dock), time.monotonic()
            last[0] = last[1] = time.perf_counter()
            timer.start()
            QTimer.singleShot(int(args.seconds * 1000), qt_app.quit)
            qt_app.exec()
            timer.stop()
            if stacks is not None:
                faulthandler.cancel_dump_traceback_later()
                stacks.write(f"--- end of phase {name}\n")
            elapsed = time.monotonic() - start
            stats = lag_stats(lags)
            if lags:
                stats["max_at_s"] = round(at[lags.index(max(lags))], 1)
            stats["dock_chunks_per_s"] = round((done(dock) - before) / elapsed, 2)
            if dock is not None:
                snap = engine.status(engine_ids[dock])
                stats["dock"] = {"status": snap.status, "done": snap.chunks_done, "failed": snap.chunks_failed,
                                 "total": snap.chunks_total, "tasks_running": snap.tasks_running, "error": snap.error}
            if load is not None:
                snap = engine.status(engine_ids[load])
                stats["import"] = {"status": snap.status, "done": snap.chunks_done, "tasks_running": snap.tasks_running,
                                   "chunks_per_s": round(snap.chunks_done / elapsed, 2), "error": snap.error}
            report["phases"][name] = stats
            print(name, json.dumps(stats), flush=True)

        phase("idle", None)
        dock = rt.docking.run(receptor_set=receptor_set, exhaustiveness=1, num_modes=1, seed=1,
                              batch_size=args.batch)
        time.sleep(5)  # let the workers come up
        phase("dock", dock)
        t = time.monotonic()
        (load,) = rt.loader.load_ligands([big])
        report["import_submit_s"] = round(time.monotonic() - t, 2)
        phase("dock+import", dock, load)
        for job_id in (load, dock):
            rt.cancel_job(job_id)
    finally:
        from shiboken6 import isValid

        if window is not None and isValid(window):
            window.close()
        rt.shutdown()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    line = json.dumps(report)
    print(line)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
