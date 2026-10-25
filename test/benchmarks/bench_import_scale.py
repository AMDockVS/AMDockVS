"""The rows import at scale: one `load_ligands` job over a folder of .smi files, through the app.

    python test/benchmarks/bench_import_scale.py SMI_DIR [--files N] [--label NAME] [--keep]

Samples `max(id)` of `molecules` every few seconds (read-only), so the result says whether the
write rate degrades as the table grows (seconds per million, 1st vs last) and how long the job
runs after the last row lands (index builds at the end). CPU is split into the parent (sink,
orchestration) and its children (chemistry workers). One JSON line per run, appended to
test/benchmarks/results/import_scale_<date>.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[2]
SAMPLE_S = 5.0


def tree_mb(path: Path) -> float:
    return round(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6, 1)


def sampler(db: Path, stop: threading.Event, samples: list, children_cpu: dict) -> None:
    me = psutil.Process()
    start = time.monotonic()
    while not stop.wait(SAMPLE_S):
        for child in me.children(recursive=True):
            try:
                cpu = child.cpu_times()
                children_cpu[child.pid] = cpu.user + cpu.system
            except psutil.Error:
                pass
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            n = con.execute("SELECT max(id) FROM molecules").fetchone()[0] or 0
            con.close()
        except sqlite3.Error:
            continue
        samples.append((round(time.monotonic() - start, 1), n))


def per_million(samples: list) -> list[float]:
    """Seconds spent on each successive million rows."""
    out, mark, last_t = [], 1_000_000, 0.0
    for t, n in samples:
        while n >= mark:
            out.append(round(t - last_t, 1))
            last_t, mark = t, mark + 1_000_000
    return out


def final_state(db: Path) -> tuple[int, list[str]]:
    """(rows, molecules index names); a DuckDB project.db is read with duckdb."""
    if db.open("rb").read(16).startswith(b"SQLite format 3"):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        index_sql = "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='molecules' ORDER BY name"
    else:
        import duckdb
        con = duckdb.connect(str(db), read_only=True)
        index_sql = "SELECT index_name FROM duckdb_indexes() WHERE table_name='molecules' ORDER BY 1"
    try:
        rows = con.execute("SELECT count(*) FROM molecules").fetchone()[0]
        return rows, [r[0] for r in con.execute(index_sql).fetchall()]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("smi_dir", type=Path)
    ap.add_argument("--files", type=int, default=0, help="first N files only (sorted); 0 = all")
    ap.add_argument("--label", default="")
    ap.add_argument("--keep", action="store_true", help="keep the project (to inspect it)")
    ap.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()))
    args = ap.parse_args()
    files = sorted(args.smi_dir.rglob("*.smi"))
    files = files[: args.files] if args.files else files
    work = Path(tempfile.mkdtemp(prefix="bench_import_", dir=args.workdir))
    os.environ["HOME"] = str(work / "home")  # projects register here, not in the real ~/.molsuite
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    from amdockvs import AMDockVSRuntime
    import amdockvs

    project = work / "project"
    db = project / "project.db"
    samples: list = []
    children_cpu: dict = {}
    stop = threading.Event()
    rt = AMDockVSRuntime()
    rt.create_project(name="bench_import", folder=project)
    thread = threading.Thread(target=sampler, args=(db, stop, samples, children_cpu), daemon=True)
    parent0 = psutil.Process().cpu_times()
    start = time.monotonic()
    thread.start()
    try:
        jobs = rt.loader.load_ligands(files)
        rt.wait_for_jobs(jobs, timeout_s=6 * 3600)
        os.sync()
        wall = time.monotonic() - start
    finally:
        stop.set()
        thread.join()
        parent = psutil.Process().cpu_times()
        rt.close_project()
    rows, molecule_indexes = final_state(db)
    last_growth = next((t for (t, n), (_, prev) in zip(reversed(samples), reversed(samples[:-1])) if n > prev), wall)
    head = subprocess.run(["git", "-C", str(Path(amdockvs.__file__).parents[2]), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    result = {
        "label": args.label, "amdockvs": head, "files": len(files), "molecules": rows,
        "wall_s": round(wall, 1), "s_per_million": per_million(samples),
        "tail_after_last_row_s": round(wall - last_growth, 1),
        "parent_cpu_s": round(parent.user + parent.system - parent0.user - parent0.system, 1),
        "children_cpu_s": round(sum(children_cpu.values()), 1),
        "project_db_mb": round(db.stat().st_size / 1e6, 1),
        "volumes_mb": tree_mb(project / "volumes") if (project / "volumes").exists() else 0,
        "molecule_indexes": molecule_indexes,
    }
    print(json.dumps(result), flush=True)
    out = REPO / "test" / "benchmarks" / "results" / f"import_scale_{date.today()}.jsonl"
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result) + "\n")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
