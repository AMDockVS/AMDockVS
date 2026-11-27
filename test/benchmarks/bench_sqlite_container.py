"""Shard container: big Parquet files vs SQLite files of blobs (2026-11-18).

    PYTHONPATH=src:../ms_flow/src python test/benchmarks/bench_sqlite_container.py \
        --records 1000000 --root /home/mario/bench_container --out results.jsonl

Same 1M SDF records (Enamine H100K cycled), one logical shard = 256 records:

  parquet         files of 1000 row groups, row group = shard, stats on `id`, one fsync per file
  sqlite          one SQLite file, (id INTEGER PRIMARY KEY, data BLOB), per-record zstd,
                  WAL + synchronous=FULL, one commit per shard: every shard durable as written
  sqlite_dict     the same with a zstd dictionary trained on 10k records
  sqlite_batch    the same as sqlite, one commit per 10k records

Write includes compression (a real worker would compress before sending). Reads are cold
(POSIX_FADV_DONTNEED): 50 random tasks of 256 / 1000 / 32000 consecutive records, decompressed,
and the whole store.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_shard_layout import base_records, fadvise_cold  # noqa: E402

PER_SHARD, RG_PER_FILE = 256, 1000
SCHEMA = pa.schema([("id", pa.uint64()), ("record", pa.binary())])


def files_in(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.is_file())


def write_parquet(d: Path, recs, n: int) -> None:
    per_file = PER_SHARD * RG_PER_FILE
    for f0 in range(0, n, per_file):
        path = d / f"vol_{f0 // per_file:05d}.parquet"
        with open(path, "wb") as fh:
            w = pq.ParquetWriter(fh, SCHEMA, compression="zstd", write_statistics=["id"])
            for lo in range(f0, min(n, f0 + per_file), PER_SHARD):
                hi = min(n, lo + PER_SHARD, f0 + per_file)
                w.write_table(pa.table({"id": pa.array(range(lo, hi), pa.uint64()),
                                        "record": [recs[i % len(recs)] for i in range(lo, hi)]}, schema=SCHEMA),
                              row_group_size=PER_SHARD)
            w.close()
            fh.flush()
            os.fsync(fh.fileno())


def write_sqlite(d: Path, recs, n: int, commit_every: int, cctx) -> None:
    con = sqlite3.connect(d / "store.sqlite", isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("CREATE TABLE r (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    for lo in range(0, n, commit_every):
        con.execute("BEGIN")
        con.executemany("INSERT INTO r VALUES (?, ?)",
                        ((i, cctx.compress(recs[i % len(recs)])) for i in range(lo, min(n, lo + commit_every))))
        con.execute("COMMIT")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


class ParquetReader:
    def __init__(self, d: Path):
        self.files = files_in(d)
        self.meta: dict[Path, pq.FileMetaData] = {}  # footer cached per worker

    def read(self, lo: int, hi: int) -> int:
        per_file, got = PER_SHARD * RG_PER_FILE, 0
        for f in range(lo // per_file, (hi - 1) // per_file + 1):
            path = self.files[f]
            meta = self.meta.get(path) or self.meta.setdefault(path, pq.read_metadata(path))
            base = f * per_file
            rgs = list(range(max(0, lo - base) // PER_SHARD, (min(hi, base + per_file) - 1 - base) // PER_SHARD + 1))
            t = pq.ParquetFile(path, metadata=meta).read_row_groups(rgs, columns=["record"])
            got += sum(len(x) for x in t.column("record").to_pylist())
        return got


class SqliteReader:
    def __init__(self, d: Path, dctx):
        self.con = sqlite3.connect(f"file:{d / 'store.sqlite'}?immutable=1", uri=True)
        self.dctx = dctx

    def read(self, lo: int, hi: int) -> int:
        return sum(len(self.dctx.decompress(b)) for (b,) in
                   self.con.execute("SELECT data FROM r WHERE id >= ? AND id < ?", (lo, hi)))


def bench(variant: str, root: Path, recs, n: int) -> dict:
    d = root / variant
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    os.sync()
    dict_data = None
    if variant == "sqlite_dict":
        dict_data = zstandard.train_dictionary(112_640, recs[:10_000])
    cctx = zstandard.ZstdCompressor(level=3, dict_data=dict_data)
    t = time.perf_counter()
    if variant == "parquet":
        write_parquet(d, recs, n)
    else:
        write_sqlite(d, recs, n, 10_000 if variant == "sqlite_batch" else PER_SHARD, cctx)
    os.sync()
    res = {"variant": variant, "records": n, "write_s": round(time.perf_counter() - t, 2)}
    files = files_in(d)
    res["files"], res["mb"] = len(files), round(sum(f.stat().st_size for f in files) / 1e6, 1)

    def reader():
        return ParquetReader(d) if variant == "parquet" else SqliteReader(
            d, zstandard.ZstdDecompressor(dict_data=dict_data))

    rnd = random.Random(7)
    for size in (s for s in (PER_SHARD, 1000, 32_000) if s < n):
        starts = [rnd.randrange(0, n - size) for _ in range(50)]
        fadvise_cold(files)
        r = reader()
        t = time.perf_counter()
        for s in starts:
            r.read(s, s + size)
        res[f"task{size}_cold_ms"] = round(1000 * (time.perf_counter() - t) / len(starts), 2)
    fadvise_cold(files)
    r = reader()
    t = time.perf_counter()
    total = sum(r.read(lo, min(n, lo + 100_000)) for lo in range(0, n, 100_000))
    res["read_all_s"] = round(time.perf_counter() - t, 2)
    res["read_all_mb"] = round(total / 1e6, 1)
    shutil.rmtree(d)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=1_000_000)
    ap.add_argument("--variants", nargs="+", default=["parquet", "sqlite", "sqlite_dict", "sqlite_batch"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    recs = base_records("sdf")
    for v in a.variants:
        r = bench(v, a.root, recs, a.records)
        print(json.dumps(r), flush=True)
        with a.out.open("a") as fh:
            fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
