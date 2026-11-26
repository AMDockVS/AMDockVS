"""Records per shard on the container format (2026-11-26): what the size changes after the import.

    PYTHONPATH=src:<ms_flow>/src python test/benchmarks/bench_shard_size.py \
        --smi /home/mario/bench_shardfiles/in/lib1m.smi --root /home/mario/bench_shardsize \
        --out results.jsonl --sizes 64 128 256 512 1024

Per size: the library imported through `loader.shard_ligands` (compute executor), then one result
part pair per library shard written by 14 worker processes (`ResultFragmentWriter`: the rows of
a docked shard, a real Enamine SDF block as each pose), committed by this process through the
`PairWriter` of the key in groups of 64 as they come (what `ShardHitWriter` does), then the Top
1000 picked (`hit_refs`, cold) and their poses read (`load_hits`). The import alone is
`bench_shard_files_app.py --sizes ...`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SDF = Path(os.environ.get("BENCH_SDF") or Path(__file__).resolve().parents[2] / "data" / "Enamine_H100K" / "Enamine_H100K.sdf")
WORKERS = 14
GROUP = 64  # receipts a commit takes, as a delivery hands them
_POSES: list[str] = []


def _poses() -> list[str]:
    if not _POSES:
        with open(SDF) as fh:
            block = []
            for line in fh:
                block.append(line)
                if line.startswith("$$$$"):
                    _POSES.append("".join(block))
                    block = []
                    if len(_POSES) == 2000:
                        break
    return _POSES


def write_shards(job: list[dict]) -> list[dict]:
    """A worker's task: each library shard's docking rows as the two parts of one result."""
    from amdockvs.docking.results.dataset import ResultFragmentWriter, payload_part

    poses, rng, receipts = _poses(), random.Random(job[0]["shard_id"]), []
    for shard in job:
        path = Path(shard["parts"]) / f"shard_{shard['shard_id']}.arrows"
        with ResultFragmentWriter(path, metadata={"input_key": "bench"}, origin=shard["shard_id"],
                                  base_id=shard["base_id"], slot_count=shard["slot_count"]) as writer:
            for ligand in range(shard["base_id"], shard["base_id"] + shard["n_records"]):
                writer.add({"ligand_id": ligand, "shard_id": shard["shard_id"], "receptor_id": 1,
                            "score": -rng.uniform(4, 12), "score_type": "vina", "status": "done",
                            "engine": "vina", "smiles": "C", "metrics_json": "{}",
                            "pose_text": poses[ligand % len(poses)], "pose_suffix": ".sdf"})
        receipts.append({"part": f"bench:{shard['shard_id']}", "path": str(path), "bulk": str(payload_part(path))})
    return receipts


def cold(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file():
            fd = os.open(path, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)


def bench(smi: Path, size: int, root: Path) -> dict:
    from ms_flow.core.data import catalog

    from amdockvs.docking.results.dataset import hit_refs, load_hits, result_roles
    from amdockvs.runtime import AMDockVSRuntime

    d = root / f"size_{size}"
    shutil.rmtree(d, ignore_errors=True)
    for name, var in (("home", "HOME"), ("molsuite", "MOLSUITE_HOME"), ("amdock", "AMDOCK_HOME")):
        os.environ[var] = str(d / name)
    os.environ["AMDOCK_OFFLINE"] = "1"
    rt = AMDockVSRuntime()
    try:
        rt.create_project(name="bench", folder=d / "project")
        db = rt.workspace.project_db
        t = time.perf_counter()
        rt.wait_for_jobs(list(rt.loader.shard_ligands([str(smi)], shard_size=size)), timeout_s=3600)
        res = {"shard_size": size, "import_s": round(time.perf_counter() - t, 2)}
        key, folder = "bench", d / "project" / "docking" / "results" / "bench"
        parts = folder / "parts"
        parts.mkdir(parents=True)
        shards = [{"shard_id": int(r["id"]), "base_id": int(r["base_id"]), "slot_count": int(r["slot_count"]),
                   "n_records": int(r["n_records"]), "parts": str(parts)} for r in catalog.shards(db, whole=True)]
        per_task = max(1, 1024 // size)  # a task of ~1024 ligands, as the autobatch would grow it
        jobs = [shards[i:i + per_task] for i in range(0, len(shards), per_task)]
        res.update(shards=len(shards), records=sum(s["n_records"] for s in shards), tasks=len(jobs))
        commit_s, held, t = 0.0, [], time.perf_counter()
        generation = catalog.active_generation_id(db)  # the library's, as a docked shard's envelope says
        with catalog.PairWriter(db, folder, generation_id=generation, roles=result_roles(key), prefix="bench:") as writer, \
                ProcessPoolExecutor(WORKERS) as pool:
            for future in as_completed([pool.submit(write_shards, job) for job in jobs]):
                held += future.result()
                if len(held) >= GROUP:
                    c = time.perf_counter()
                    writer.commit_parts(held)
                    commit_s += time.perf_counter() - c
                    held = []
            if held:
                c = time.perf_counter()
                writer.commit_parts(held)
                commit_s += time.perf_counter() - c
        res.update(results_s=round(time.perf_counter() - t, 2), commit_s=round(commit_s, 2),
                   parts_per_s=round(len(shards) / commit_s, 1) if commit_s else None)
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix != ".lock"]
        res.update(result_containers=len([p for p in files if p.suffix == ".arrows"]),
                   result_mb=round(sum(p.stat().st_size for p in files) / 1e6, 1),
                   index_kb=round(sum(p.stat().st_size for p in files if p.suffix == ".index") / 1e3, 1),
                   parts_left=len(list(parts.iterdir())))
        cold(folder)
        t = time.perf_counter()
        refs = hit_refs(db, keys=[key], top_n=1000)
        res["top_s"] = round(time.perf_counter() - t, 3)
        t = time.perf_counter()
        hits = load_hits(refs)
        res.update(poses_s=round(time.perf_counter() - t, 3), hits=len(hits))
        return res
    finally:
        rt.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smi", type=Path, required=True)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sizes", nargs="+", type=int, default=[64, 128, 256, 512, 1024])
    a = ap.parse_args()
    for size in a.sizes:
        r = bench(a.smi, size, a.root)
        print(json.dumps(r), flush=True)
        with a.out.open("a") as fh:
            fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
