"""Many small shard files vs a few big Parquet files read by row group (2026-11-18).

    PYTHONPATH=src:../ms_flow/src python test/benchmarks/bench_shard_layout.py \
        --payload smiles --shards 1000 10000 100000 --root /home/mario/bench_shards

A *logical shard* is always `--per-shard` records (1000, today's default). Layouts:

  A   one file per shard, written by ms_flow's real `ShardWriter` (fsync file + dir per shard)
  A0  same files, no per-file fsync; one `os.sync()` at the end
  B   big files of `--big-records` records, one row group per logical shard, one fsync per file

Per layout: durable write, listing (scandir+stat), cold read-all, cold random tasks of 1 and of
32 shards (autobatch stack), local rsync copy, rm. Cold = POSIX_FADV_DONTNEED on every data
file (page cache only; dentry/inode caches need root and stay warm). One JSON line per layout.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ms_flow.core.data.shard import PayloadKind, Serializer, Shard, ShardWriter

SDF = Path(__file__).resolve().parents[2] / "data" / "Enamine_H100K" / "Enamine_H100K.sdf"
SCHEMA = pa.schema([("id", pa.uint64()), ("record", pa.string())])


def base_records(payload: str) -> list[bytes]:
    blocks = [b + "$$$$\n" for b in SDF.read_text().split("$$$$\n") if b.strip()]
    if payload == "sdf":
        return [b.encode() for b in blocks]
    from rdkit import Chem, RDLogger
    RDLogger.DisableLog("rdApp.*")
    out = []
    for b in blocks:
        m = Chem.MolFromMolBlock(b)
        if m is not None:
            out.append(Chem.MolToSmiles(m).encode())
    return out


def shard_records(base: list[bytes], shard: int, per: int) -> list[bytes]:
    start = (shard * per) % len(base)
    return [base[(start + i) % len(base)] for i in range(per)]


def fadvise_cold(files: list[Path]) -> None:
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def data_files(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.suffix == ".parquet")


# --- writers ---------------------------------------------------------------------------------

def write_small(d: Path, base, n, per, fsync: bool, kind: int) -> None:
    import ms_flow.core.data.shard as shard_mod
    real = shard_mod.os.fsync, shard_mod._fsync_dir
    if not fsync:  # A0: the real writer with both fsyncs turned off
        shard_mod.os.fsync, shard_mod._fsync_dir = (lambda fd: None), (lambda d: None)
    try:
        _write_small(d, base, n, per, kind)
    finally:
        shard_mod.os.fsync, shard_mod._fsync_dir = real


def _write_small(d: Path, base, n, per, kind: int) -> None:
    for s in range(n):
        recs = shard_records(base, s, per)
        w = ShardWriter(d / f"shard_{s:06d}.parquet", dataset_id=None, shard_id=s, base_id=s * per,
                        kind=kind, serializer=Serializer.UTF8, slot_count=per,
                        metadata={"import_batch_id": 1, "source_indices": list(range(s * per, s * per + per))})
        for i, r in enumerate(recs):
            w.add(s * per + i, r)
        w.close()


def write_big(d: Path, base, n, per, big: int) -> None:
    per_file = big // per
    for f0 in range(0, n, per_file):
        path = d / f"vol_{f0 // per_file:05d}.parquet"
        with open(path, "wb") as fh:
            w = pq.ParquetWriter(fh, SCHEMA, compression="zstd", write_page_checksum=True)
            for s in range(f0, min(n, f0 + per_file)):
                recs = shard_records(base, s, per)
                w.write_table(pa.table({"id": pa.array(range(s * per, s * per + per), pa.uint64()),
                                        "record": pa.array(recs, pa.binary()).cast(pa.string())}),
                              row_group_size=per)
            w.close()
            fh.flush()
            os.fsync(fh.fileno())
    fd = os.open(d, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


# --- readers: return bytes of record payload read ---------------------------------------------

def read_small(path: Path) -> int:
    with Shard.open(path) as sh:
        return pc.sum(pc.binary_length(sh._records)).as_py()


def read_big(path: Path, rgs: list[int]) -> int:
    with pq.ParquetFile(path, page_checksum_verification=True) as pf:
        t = pf.read_row_groups(rgs, columns=["id", "record"])
    t.column("id").to_pylist()  # same work Shard.open does on ids
    return pc.sum(pc.binary_length(t.column("record"))).as_py()


def locate(layout: str, d: Path, shard: int, per_file: int) -> tuple[Path, int]:
    if layout == "B":
        return d / f"vol_{shard // per_file:05d}.parquet", shard % per_file
    return d / f"shard_{shard:06d}.parquet", 0


def task(layout, d, shards: list[int], per_file) -> int:
    if layout != "B":
        return sum(read_small(locate(layout, d, s, per_file)[0]) for s in shards)
    by_file: dict[Path, list[int]] = {}
    for s in shards:
        p, rg = locate(layout, d, s, per_file)
        by_file.setdefault(p, []).append(rg)
    return sum(read_big(p, rgs) for p, rgs in by_file.items())


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return time.perf_counter() - t, out


def run_layout(layout, root: Path, base, n, per, big, payload, kind) -> dict:
    d = root / f"{payload}_{n}_{layout}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    os.sync()
    per_file = big // per
    res = {"payload": payload, "shards": n, "records": n * per, "layout": layout}

    if layout == "B":
        res["write_s"], _ = timed(lambda: write_big(d, base, n, per, big))
    else:
        res["write_s"], _ = timed(lambda: write_small(d, base, n, per, layout == "A", kind))
    if layout == "A0":
        res["write_s"] += timed(os.sync)[0]
    files = data_files(d)
    res["files"] = len(files)
    res["bytes"] = sum(f.stat().st_size for f in files)

    res["list_stat_s"], _ = timed(lambda: sum(e.stat().st_size for e in os.scandir(d)))

    fadvise_cold(files)
    if layout == "B":
        res["read_all_s"], got = timed(lambda: sum(read_big(f, list(range(pq.ParquetFile(f).num_row_groups)))
                                                   for f in files))
    else:
        res["read_all_s"], got = timed(lambda: sum(read_small(f) for f in files))
    res["read_all_MBps"] = round(got / 1e6 / res["read_all_s"], 1)

    rnd = random.Random(7)
    for k, ntasks in ((1, 300), (32, 50)):
        starts = [rnd.randrange(0, max(1, n - k)) for _ in range(ntasks)]
        fadvise_cold(files)
        t, _ = timed(lambda: [task(layout, d, list(range(s, min(n, s + k))), per_file) for s in starts])
        res[f"task{k}_cold_ms"] = round(1000 * t / ntasks, 2)
        t, _ = timed(lambda: [task(layout, d, list(range(s, min(n, s + k))), per_file) for s in starts])
        res[f"task{k}_warm_ms"] = round(1000 * t / ntasks, 2)

    if layout == "B":
        fadvise_cold(files)
        t, _ = timed(lambda: [pq.ParquetFile(f).metadata for f in files])
        res["footer_open_ms"] = round(1000 * t / len(files), 2)
        res["footer_bytes"] = pq.ParquetFile(files[0]).metadata.serialized_size

    dst = root / f"{d.name}_copy"
    shutil.rmtree(dst, ignore_errors=True)
    res["rsync_s"], _ = timed(lambda: (subprocess.run(["rsync", "-a", f"{d}/", f"{dst}/"], check=True), os.sync()))
    res["rm_s"], _ = timed(lambda: shutil.rmtree(dst))
    shutil.rmtree(d)
    return {k: round(v, 3) if isinstance(v, float) else v for k, v in res.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", choices=["smiles", "sdf"], default="smiles")
    ap.add_argument("--shards", type=int, nargs="+", default=[1000, 10000])
    ap.add_argument("--per-shard", type=int, default=1000)
    ap.add_argument("--big-records", type=int, default=1_000_000)
    ap.add_argument("--layouts", nargs="+", default=["A", "A0", "B"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    base = base_records(a.payload)
    kind = PayloadKind.SMILES if a.payload == "smiles" else PayloadKind.SDF
    for n in a.shards:
        for layout in a.layouts:
            r = run_layout(layout, a.root, base, n, a.per_shard, a.big_records, a.payload, kind)
            r["big_records"] = a.big_records
            print(json.dumps(r), flush=True)
            with a.out.open("a") as fh:
                fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
