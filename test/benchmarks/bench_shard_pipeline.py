"""Shard pipeline on big Parquet files vs today's one-file-per-shard (2026-11-18).

    PYTHONPATH=src:../ms_flow/src:../ms_components/src:../ms_env/src:../ms_adtools/src:../ms_contactmap/src \
    python test/benchmarks/bench_shard_pipeline.py --root /home/mario/bench_shards --out results.jsonl \
        --inputs 1000000 10000000 --results 1000000

inputs   library layout and task granularity (can a shard be cut?):
           A        today: one file of 1000 records per shard (ms_flow ShardWriter)
           B<rg>    files of <= 1000 row groups, row group = <rg> records, stats on `id` only,
                    footer read once per worker (cached), a task = a run of row groups
results  docking output path, 14 worker processes, tasks of 1000 ligands (today) or 128:
           R0       today: ResultFragmentWriter per task file (one row group per ligand)
           R1       per-task file, one row group per task
           R2       workers return Arrow tables; the parent appends one row group per task to a
                    rolling file (<= 1000 row groups, fsync on rotate)
           adopt    R1 files appended into the parent's rolling file (what comes back from an HPC)
         then ranking: top 1000 by score (summary columns only) + fetching those 1000 poses.

Records are real Enamine H100K (SMILES ~70 B; SDF ~2.4 KB stands in for PDBQT and for a pose).
Cold = POSIX_FADV_DONTNEED on the data files (dentry/inode caches stay warm).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ms_flow.core.data.shard import Serializer, Shard, ShardWriter
from amdockvs.core.constants import ShardKind

SDF = Path(__file__).resolve().parents[2] / "data" / "Enamine_H100K" / "Enamine_H100K.sdf"
IN_SCHEMA = pa.schema([("id", pa.uint64()), ("record", pa.string())])
MAX_RG_PER_FILE = 1000
WORKERS = 14
_BASE: dict[str, list[bytes]] = {}


# --- helpers ---------------------------------------------------------------------------------

def base_records(payload: str) -> list[bytes]:
    if payload not in _BASE:
        blocks = [b + "$$$$\n" for b in SDF.read_text().split("$$$$\n") if b.strip()]
        if payload == "sdf":
            _BASE[payload] = [b.encode() for b in blocks]
        else:
            from rdkit import Chem, RDLogger
            RDLogger.DisableLog("rdApp.*")
            mols = (Chem.MolFromMolBlock(b) for b in blocks)
            _BASE[payload] = [Chem.MolToSmiles(m).encode() for m in mols if m is not None]
    return _BASE[payload]


def records(base, start: int, n: int) -> list[bytes]:
    return [base[(start + i) % len(base)] for i in range(n)]


def cold(files) -> None:
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)


def fsync_path(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


def parquets(d: Path) -> list[Path]:
    return sorted(d.rglob("*.parquet"))


def size(d: Path) -> int:
    return sum(f.stat().st_size for f in parquets(d))


def clock(fn):
    t = time.perf_counter()
    out = fn()
    return round(time.perf_counter() - t, 3), out


# --- inputs ----------------------------------------------------------------------------------

def write_A(d: Path, base, n: int, kind: int) -> None:
    for s in range(n // 1000):
        with ShardWriter(d / f"shard_{s:06d}.parquet", dataset_id=None, shard_id=s, base_id=s * 1000,
                         kind=kind, serializer=Serializer.UTF8, slot_count=1000,
                         metadata={"import_batch_id": 1, "source_indices": list(range(s * 1000, s * 1000 + 1000))}) as w:
            for i, r in enumerate(records(base, s * 1000, 1000)):
                w.add(s * 1000 + i, r)


def write_B(d: Path, base, n: int, rg: int) -> None:
    per_file = rg * MAX_RG_PER_FILE
    for f0 in range(0, n, per_file):
        path = d / f"lib_{f0 // per_file:05d}.parquet"
        with open(path, "wb") as fh:
            w = pq.ParquetWriter(fh, IN_SCHEMA, compression="zstd", write_page_checksum=True,
                                 write_statistics=["id"])
            for s in range(f0, min(n, f0 + per_file), rg):
                k = min(rg, n - s)
                w.write_table(pa.table({"id": pa.array(range(s, s + k), pa.uint64()),
                                        "record": pa.array(records(base, s, k), pa.binary()).cast(pa.string())}),
                              row_group_size=rg)
            w.close()
            fh.flush()
            os.fsync(fh.fileno())
    fsync_path(d)


class BReader:
    """A worker's view of a B library: footers read once, then row groups by id range."""

    def __init__(self, d: Path, rg: int):
        self.d, self.rg, self.per_file, self.meta = d, rg, rg * MAX_RG_PER_FILE, {}

    def read(self, start: int, n: int) -> int:
        got = 0
        while n > 0:
            fi, off = divmod(start, self.per_file)
            path = self.d / f"lib_{fi:05d}.parquet"
            if path not in self.meta:
                self.meta[path] = pq.read_metadata(path)
            first, last = off // self.rg, (off + n - 1) // self.rg
            last = min(last, self.meta[path].num_row_groups - 1)
            with pq.ParquetFile(path, metadata=self.meta[path], page_checksum_verification=True) as pf:
                t = pf.read_row_groups(list(range(first, last + 1)))
            ids = t.column("id").to_numpy()
            keep = pc.and_(pc.greater_equal(t.column("id"), start), pc.less(t.column("id"), start + n))
            got += pc.sum(pc.binary_length(t.column("record").filter(keep))).as_py() or 0
            done = int(ids[-1]) + 1 - start
            start, n = start + done, n - done
        return got


def read_A(d: Path, start: int, n: int) -> int:
    got = 0
    for s in range(start // 1000, (start + n - 1) // 1000 + 1):
        with Shard.open(d / f"shard_{s:06d}.parquet") as sh:  # the whole shard, whatever the slice
            ids = pa.array(sh._ids, pa.uint64())
            keep = pc.and_(pc.greater_equal(ids, start), pc.less(ids, start + n))
            got += pc.sum(pc.binary_length(sh._records.filter(keep))).as_py() or 0
    return got


def bench_inputs(root: Path, payload: str, n: int, out) -> None:
    base = base_records(payload)
    kind = ShardKind.SMILES if payload == "smiles" else ShardKind.SDF
    for layout in ("A", "B128", "B256", "B1000"):
        d = root / f"in_{payload}_{n}_{layout}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        os.sync()
        r = {"bench": "inputs", "payload": payload, "records": n, "layout": layout}
        if layout == "A":
            r["write_s"], _ = clock(lambda: write_A(d, base, n, kind))
            reader = lambda s, k: read_A(d, s, k)
        else:
            rg = int(layout[1:])
            r["write_s"], _ = clock(lambda: write_B(d, base, n, rg))
            br = BReader(d, rg)
            reader = br.read
            files = parquets(d)
            cold(files)
            meta = pq.read_metadata(files[0])
            r["footer_kb"] = round(meta.serialized_size / 1024)
            r["footer_open_ms"], _ = clock(lambda: [pq.read_metadata(f) for f in files])
            r["footer_open_ms"] = round(1000 * r["footer_open_ms"] / len(files), 2)
        files = parquets(d)
        r["files"], r["bytes"] = len(files), size(d)
        cold(files)
        if layout == "A":
            r["read_all_s"], got = clock(lambda: sum(read_A(d, s, 1000) for s in range(0, n, 1000)))
        else:
            r["read_all_s"], got = clock(lambda: sum(BReader(d, rg).read(s, 100_000) for s in range(0, n, 100_000)))
        r["read_all_MBps"] = round(got / 1e6 / r["read_all_s"], 1)
        rnd = random.Random(11)
        for k, ntask in ((128, 300), (1000, 200), (32_000, 20)):
            starts = [rnd.randrange(0, n - k) for _ in range(ntask)]
            if layout != "A":
                br.read(0, 1)  # the worker's footers: once, before its first task
                for f in files:
                    br.meta.setdefault(f, pq.read_metadata(f))
            cold(files)
            t, _ = clock(lambda: [reader(s, k) for s in starts])
            r[f"task{k}_cold_ms"] = round(1000 * t / ntask, 2)
        emit(r, out)
        shutil.rmtree(d)


# --- results ---------------------------------------------------------------------------------

def result_rows(start: int, n: int) -> list[dict]:
    base = base_records("sdf")
    smi = base_records("smiles")
    rnd = random.Random(start)
    return [{"ligand_id": start + i, "receptor_id": 1, "score": round(rnd.gauss(-7.0, 1.5), 3),
             "score_type": "vina", "status": "ok", "engine": "vina", "has_pose": True,
             "smiles": smi[(start + i) % len(smi)].decode(), "metrics_json": "{}",
             "pose_text": base[(start + i) % len(base)].decode(), "pose_suffix": ".sdf"}
            for i in range(n)]


def task_R0(args) -> str:
    from amdockvs.docking.results.dataset import ResultFragmentWriter
    d, start, n = args
    path = Path(d) / f"shard_{start // n:06d}.results.parquet"
    with ResultFragmentWriter(path, metadata={"shard_id": start // n}) as w:
        for row in result_rows(start, n):
            w.add(row)
    return str(path)


def task_R1(args) -> str:
    from amdockvs.docking.results.dataset import RESULT_SCHEMA
    d, start, n = args
    path = Path(d) / f"task_{start:09d}.results.parquet"
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(pa.Table.from_pylist(result_rows(start, n), schema=RESULT_SCHEMA), tmp,
                   compression="zstd", write_statistics=["ligand_id"])
    os.replace(tmp, path)
    return str(path)


def task_R2(args) -> pa.Table:
    from amdockvs.docking.results.dataset import RESULT_SCHEMA
    _d, start, n = args
    return pa.Table.from_pylist(result_rows(start, n), schema=RESULT_SCHEMA)


class RollingWriter:
    """The parent's sink: one row group per task, a new file every MAX_RG_PER_FILE, fsync on rotate."""

    def __init__(self, d: Path, schema):
        self.d, self.schema, self.n_file, self.rgs, self.w, self.fh = d, schema, 0, 0, None, None
        self.busy = 0.0

    def add(self, t: pa.Table) -> None:
        t0 = time.perf_counter()
        if self.w is None:
            self.fh = open(self.d / f"res_{self.n_file:05d}.parquet.tmp", "wb")
            self.w = pq.ParquetWriter(self.fh, self.schema, compression="zstd", write_statistics=["ligand_id"])
        self.w.write_table(t, row_group_size=len(t))
        self.rgs += 1
        if self.rgs >= MAX_RG_PER_FILE:
            self.close()
        self.busy += time.perf_counter() - t0

    def close(self) -> None:
        if self.w is None:
            return
        self.w.close()
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()
        tmp = self.d / f"res_{self.n_file:05d}.parquet.tmp"
        os.replace(tmp, tmp.with_suffix(""))
        fsync_path(self.d)
        self.n_file, self.rgs, self.w = self.n_file + 1, 0, None


def rank(d: Path) -> tuple[float, float]:
    files = parquets(d)
    cold(files)
    t0 = time.perf_counter()
    tables = [pq.read_table(f, columns=["ligand_id", "receptor_id", "score", "status"]) for f in files]
    allr = pa.concat_tables(tables)
    top = allr.take(pc.select_k_unstable(allr, 1000, [("score", "ascending")]))
    t_rank = time.perf_counter() - t0
    want = sorted(set(top.column("ligand_id").to_pylist()))
    cold(files)
    t0 = time.perf_counter()
    got = 0
    for f in files:  # each file's row groups whose ligand_id range holds a wanted id
        meta = pq.read_metadata(f)
        rgs = []
        for i in range(meta.num_row_groups):
            st = meta.row_group(i).column(0).statistics
            if st is None or bisect_left(want, st.max + 1) > bisect_left(want, st.min):
                rgs.append(i)
        if not rgs:
            continue
        with pq.ParquetFile(f, metadata=meta) as pf:
            t = pf.read_row_groups(rgs, columns=["ligand_id", "pose_text"])
        mask = pc.is_in(t.column("ligand_id"), pa.array(want, pa.int64()))
        got += pc.sum(mask).as_py()
    assert got == 1000, got
    return round(t_rank, 3), round(time.perf_counter() - t0, 3)


def bench_results(root: Path, n: int, out, variants) -> None:
    from amdockvs.docking.results.dataset import RESULT_SCHEMA
    for variant, per_task in (("R0", 1000), ("R1", 1000), ("R1", 128), ("R2", 1000), ("R2", 128)):
        if variant not in variants:
            continue
        d = root / f"res_{n}_{variant}_{per_task}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        os.sync()
        args = [(str(d), s, min(per_task, n - s)) for s in range(0, n, per_task)]
        r = {"bench": "results", "records": n, "variant": variant, "task_ligands": per_task, "tasks": len(args)}
        with ProcessPoolExecutor(WORKERS) as pool:
            list(pool.map(task_R2, args[:WORKERS]))  # warm the workers' imports and records
            t0 = time.perf_counter()
            if variant == "R2":
                sink = RollingWriter(d, RESULT_SCHEMA)
                for table in pool.map(task_R2, args, chunksize=1):
                    sink.add(table)
                sink.close()
                r["parent_busy_s"] = round(sink.busy, 3)
            else:
                list(pool.map(task_R0 if variant == "R0" else task_R1, args, chunksize=1))
                os.sync()
            r["write_s"] = round(time.perf_counter() - t0, 3)
        r["files"], r["bytes"] = len(parquets(d)), size(d)
        r["footer_kb"] = round(pq.read_metadata(parquets(d)[0]).serialized_size / 1024)
        if variant == "R1" and per_task == 128:  # what an HPC sends back, adopted by the parent
            files = parquets(d)
            dst = root / f"res_{n}_adopt"
            shutil.rmtree(dst, ignore_errors=True)
            dst.mkdir()
            cold(files)
            sink = RollingWriter(dst, RESULT_SCHEMA)
            t, _ = clock(lambda: [sink.add(pq.read_table(f)) for f in files] and sink.close())
            emit({"bench": "results", "records": n, "variant": "adopt", "task_ligands": per_task,
                  "files_in": len(files), "adopt_s": t, "files": len(parquets(dst)), "bytes": size(dst)}, out)
            shutil.rmtree(dst)
        r["rank_top1000_s"], r["fetch_1000_poses_s"] = rank(d)
        emit(r, out)
        shutil.rmtree(d)


def emit(r: dict, out: Path) -> None:
    print(json.dumps(r), flush=True)
    with out.open("a") as fh:
        fh.write(json.dumps(r) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--inputs", type=int, nargs="*", default=[1_000_000])
    ap.add_argument("--input-payloads", nargs="+", default=["smiles", "sdf"])
    ap.add_argument("--results", type=int, nargs="*", default=[1_000_000])
    ap.add_argument("--result-variants", nargs="+", default=["R0", "R1", "R2"])
    a = ap.parse_args()
    a.root.mkdir(parents=True, exist_ok=True)
    for n in a.inputs:
        for payload in a.input_payloads:
            bench_inputs(a.root, payload, n, a.out)
    for n in a.results:
        bench_results(a.root, n, a.out, a.result_variants)


if __name__ == "__main__":
    main()
