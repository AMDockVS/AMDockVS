"""The real app import into shards: big multi-shard Parquet files vs one file per shard (2026-11-18).

Runs whichever AMDockVS/ms_flow is on PYTHONPATH, so the same script measures both trees:

    PYTHONPATH=<amdock>/src:<ms_flow>/src:../ms_components/src:../ms_env/src:../ms_adtools/src:../ms_contactmap/src \
    python test/benchmarks/bench_shard_files_app.py --label branch --root /home/mario/bench_shardfiles \
        --out results.jsonl

Per input (1M SDF = Enamine H100K x10, 1M SMILES) and shard size: import wall time through
`loader.shard_ligands` (compute executor), files/bytes on disk, a cold read of every shard
through `ShardAPI.open`. (Preparation is selected by file now, so the 4-shard probe it had
is gone; `results/shard_files_app_2026-11-18.jsonl` keeps its `prep4_s`.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

SDF = Path(__file__).resolve().parents[2] / "data" / "Enamine_H100K" / "Enamine_H100K.sdf"


def inputs(root: Path) -> dict[str, Path]:
    d = root / "in"
    d.mkdir(parents=True, exist_ok=True)
    sdf, smi = d / "lib1m.sdf", d / "lib1m.smi"
    if not sdf.exists():
        text = SDF.read_bytes()
        with sdf.open("wb") as fh:
            for _ in range(10):
                fh.write(text)
    if not smi.exists():
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
        lines = [f"{Chem.MolToSmiles(m)} {m.GetProp('_Name')}\n"
                 for m in Chem.ForwardSDMolSupplier(str(SDF)) if m is not None]
        with smi.open("w") as fh:
            for _ in range(10):
                fh.writelines(lines)
    return {"sdf": sdf, "smiles": smi}


def cold(files: list[Path]) -> None:
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)


def bench(label: str, fmt: str, source: Path, shard_size: int, root: Path) -> dict:
    from ms_flow.core.data import catalog
    from amdockvs.runtime import AMDockVSRuntime

    d = root / f"{label}_{fmt}_{shard_size}"
    shutil.rmtree(d, ignore_errors=True)
    for name, var in (("home", "HOME"), ("molsuite", "MOLSUITE_HOME"), ("amdock", "AMDOCK_HOME")):
        os.environ[var] = str(d / name)
    os.environ["AMDOCK_OFFLINE"] = "1"
    rt = AMDockVSRuntime()
    try:
        rt.create_project(name="bench", folder=d / "project")
        os.sync()
        t = time.perf_counter()
        jobs = rt.loader.shard_ligands([str(source)], shard_size=shard_size)
        states = rt.wait_for_jobs(list(jobs), timeout_s=3600)
        res = {"label": label, "format": fmt, "shard_size": shard_size,
               "import_s": round(time.perf_counter() - t, 2),
               "status": [str(s.status) for s in states.values()]}
        rows = list(catalog.shards(rt.workspace.project_db, whole=True))
        shard_dir = Path(rt.get_project_resource_path("shards"))
        files = [p for p in shard_dir.rglob("*") if p.is_file()]
        res.update(shards=len(rows), records=sum(r["n_records"] for r in rows), files=len(files),
                   catalog_files=sum(1 for _ in catalog.files(rt.workspace.project_db)),
                   mb=round(sum(p.stat().st_size for p in files) / 1e6, 1))

        cold(files)
        t, n = time.perf_counter(), 0
        for row in rows:
            with rt.shards.open(row["id"]) as shard:
                n += sum(1 for record in shard if record.load())
        res["read_all_s"] = round(time.perf_counter() - t, 2)
        res["read_records"] = n
        return res
    finally:
        rt.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--formats", nargs="+", default=["smiles", "sdf"])
    ap.add_argument("--sizes", nargs="+", type=int, default=[1000, 256])
    a = ap.parse_args()
    src = inputs(a.root)
    for fmt in a.formats:
        for size in a.sizes:
            r = bench(a.label, fmt, src[fmt], size, a.root)
            print(json.dumps(r), flush=True)
            with a.out.open("a") as fh:
                fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
