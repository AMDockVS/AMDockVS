"""Step-by-step wall time of a small docking project, to compare two storage generations.

    python test/benchmarks/bench_docking_steps.py --label volumes --out results.jsonl \
        --receptor data/docking/1iep_receptorH.pdb@15.190,53.903,16.917 --receptor 4O71.pdb@24.659,98.749,31.691

Only the public runtime API is used, so the same file runs against an older checkout: put
that checkout's `src` (and its ms_flow) first on PYTHONPATH. Each run builds a fresh project
under a temporary HOME, times every step with the app's own executors, prints and appends one
JSON line, and deletes the project. `--summarize results.jsonl` prints the median table.
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
BOX_SIZE = (22.0, 22.0, 22.0)
STEPS = ("create_project", "import_receptors", "import_ligands", "generate_3d", "prepare_receptors",
         "prepare_ligands", "set_grid", "docking", "read_poses")


def first_records(sdf: Path, n: int, dest: Path) -> Path:
    with open(sdf, encoding="utf-8") as source, open(dest, "w", encoding="utf-8") as out:
        for line in source:
            out.write(line)
            if line.startswith("$$$$"):
                n -= 1
                if n == 0:
                    break
    return dest


def project_files(root: Path) -> tuple[int, int]:
    files = size = 0
    for folder, _dirs, names in os.walk(root):
        for name in names:
            files += 1
            size += os.path.getsize(Path(folder) / name)
    return files, size


def pose_bytes(runtime, hit, receptors_read: set[int]) -> int:
    """What the viewer reads to show a hit: its pose, and its receptor once per receptor."""
    if hasattr(hit, "pose_payload_id"):  # volumes: payload ids
        from amdockvs.core.payloads import payload_store

        store = payload_store(runtime)
        read = lambda payload_id: store.get(payload_id)
        pose, receptor = hit.pose_payload_id, hit.receptor_payload_id
    else:  # files: paths
        read = lambda path: Path(path).read_bytes()
        pose, receptor = hit.output_path, hit.receptor_path
    total = len(read(pose))
    if hit.receptor_id not in receptors_read:
        receptors_read.add(hit.receptor_id)
        total += len(read(receptor))
    return total


def run(args) -> dict:
    work = Path(tempfile.mkdtemp(prefix="bench_steps_"))
    os.environ["HOME"] = str(work / "home")  # the project registers here, not in the real ~/.molsuite
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    import amdockvs
    from amdockvs import AMDockVSRuntime

    receptors = []
    for spec in args.receptor:
        path, center = spec.rsplit("@", 1)
        receptors.append((Path(path).resolve(), tuple(float(v) for v in center.split(","))))
    ligands = first_records(args.library, args.ligands, work / "ligands.sdf")
    timeout = 3600
    steps: dict[str, float] = {}
    report = {"label": args.label, "code": str(Path(amdockvs.__file__).parents[1]),
              "receptors": len(receptors), "ligands": args.ligands, "steps": steps}

    def timed(name, action):
        start = time.monotonic()
        result = action()
        steps[name] = round(time.monotonic() - start, 2)
        return result

    rt = AMDockVSRuntime()
    try:
        project = work / "project"
        timed("create_project", lambda: rt.create_project(name="bench_steps", folder=project))
        timed("import_receptors", lambda: rt.wait_for_jobs(
            rt.loader.load_receptors([path for path, _ in receptors]), timeout_s=timeout))
        timed("import_ligands", lambda: rt.wait_for_jobs(rt.loader.load_ligands([ligands]), timeout_s=timeout))
        timed("generate_3d", lambda: rt.wait_for_jobs([rt.chemistry.generate_3d_ligands()], timeout_s=timeout))
        timed("prepare_receptors", lambda: rt.wait_for_jobs([rt.docking.prepare_receptors()], timeout_s=timeout))
        timed("prepare_ligands", lambda: rt.wait_for_jobs([rt.docking.prepare_ligands()], timeout_s=timeout))

        def set_grids():
            rows = sorted(rt.molecules.stream(rt.molecules.select(role="receptor")), key=lambda r: int(r.id))
            assert len(rows) == len(receptors), f"{len(rows)} receptors imported, {len(receptors)} given"
            for row, (_path, center) in zip(rows, receptors):
                rt.docking.set_grid(receptor_id=int(row.id), center=center, size=BOX_SIZE)
            return rt.molecules.create_set(rt.molecules.select(role="receptor"), name="bench", kind="snapshot")

        receptor_set = timed("set_grid", set_grids)
        timed("docking", lambda: rt.wait_for_jobs(
            [rt.docking.run(receptor_set=receptor_set, exhaustiveness=args.exhaustiveness, seed=1)],
            timeout_s=timeout))

        def read_poses():
            hits = rt.docking.top_hits(limit=100_000)
            seen: set[int] = set()
            return len(hits), sum(pose_bytes(rt, hit, seen) for hit in hits), hits

        report["hits"], report["read_bytes"], hits = timed("read_poses", read_poses)
        report["engine"] = sorted({str(getattr(hit, "engine", "") or "") for hit in hits})
        report["best_scores"] = sorted(round(float(hit.score), 2) for hit in hits)[:3]
        report["files"], size = project_files(project)
        report["size_mb"] = round(size / 1e6, 1)
        report["total_s"] = round(sum(steps.values()), 2)
    finally:
        rt.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    return report


def summarize(path: Path) -> str:
    runs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    labels = list(dict.fromkeys(r["label"] for r in runs))
    by = {label: [r for r in runs if r["label"] == label] for label in labels}
    med = lambda rows, key: statistics.median(r["steps"][key] for r in rows)
    head = "| step | " + " | ".join(f"{label} (s)" for label in labels) + " |"
    lines = [head, "|" + "---|" * (len(labels) + 1)]
    for step in STEPS + ("total",):
        cells = [statistics.median(r["total_s"] for r in by[label]) if step == "total" else med(by[label], step)
                 for label in labels]
        lines.append(f"| {step} | " + " | ".join(f"{value:.2f}" for value in cells) + " |")
    for key in ("hits", "files", "size_mb"):
        lines.append(f"| {key} | " + " | ".join(str(by[label][-1][key]) for label in labels) + " |")
    lines.append("")
    lines.append("Median of " + ", ".join(f"{len(by[label])} {label}" for label in labels) + " runs.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summarize", type=Path)
    ap.add_argument("--label", default="current")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--receptor", action="append", default=[], help="PDB@cx,cy,cz (box center)")
    ap.add_argument("--ligands", type=int, default=10)
    ap.add_argument("--library", type=Path, default=LIBRARY)
    ap.add_argument("--exhaustiveness", type=int, default=8)
    args = ap.parse_args()
    if args.summarize:
        print(summarize(args.summarize))
        return 0
    report = run(args)
    line = json.dumps(report)
    print(line)
    if args.out:
        with open(args.out, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
