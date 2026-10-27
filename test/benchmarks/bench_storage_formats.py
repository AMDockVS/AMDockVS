"""Storage formats for a screening library: SQLite rows + volumes vs .mshard vs Parquet.

    python test/benchmarks/bench_storage_formats.py [LIBRARY] [--stages compute,app_rows,app_mshard,storage,scale]

LIBRARY is an SDF (default data/Enamine_H100K/Enamine_H100K.sdf). Stages:

* compute — per-molecule cost of each thing the rows import computes (single core, 5k mols).
* app     — the real imports through the app: `load_ligands` (rows + volumes) and
            `shard_ligands` (.mshard); time, project.db by table/index, files.
* storage — the same precomputed records written by each format in one process, then cold
            reads: 1000 random molecules, a filter+sort over descriptors, a full stream.
* point   — ~4M molecules: one molecule, 1000 random, a filter, small appended files; Parquet
            (row groups 10k / 1k, with and without a manifest) vs volumes.
* scale   — descriptors only, 1M rows: SQLite insert with today's indexes / indexes built at
            the end / slim table, and the same filter+sort in SQLite vs DuckDB over Parquet.

Every write time includes fsync (`os.sync()`); reads start cold (fadvise DONTNEED). Each block also
reports cpu_s (all threads of this process) and peak_mb (peak RSS above the RSS at block start).
Prints one JSON line per measurement and writes them to test/benchmarks/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
LIBRARY = REPO / "data" / "Enamine_H100K" / "Enamine_H100K.sdf"
DESCRIPTORS = ("mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds")
QUERY_WHERE = "logp < 3 AND hbd <= 2 AND tpsa < 90"
QUERY = f"SELECT id FROM {{t}} WHERE {QUERY_WHERE} ORDER BY mw LIMIT 1000"
RESULTS: list[dict] = []


def emit(stage: str, variant: str, **values) -> None:
    row = {"stage": stage, "variant": variant, **values}
    RESULTS.append(row)
    print(json.dumps(row), flush=True)


def records(path: Path, limit: int | None = None):
    """Raw SDF records, `$$$$` included."""
    buf: list[str] = []
    n = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            buf.append(line)
            if line.startswith("$$$$"):
                yield "".join(buf)
                buf = []
                n += 1
                if limit and n >= limit:
                    return


def cold(paths) -> None:
    """Flush to disk, then drop these files from the page cache."""
    os.sync()
    for path in paths:
        for f in ([path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]):
            fd = os.open(f, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


class meter:
    """Wall, CPU of every thread in this process, and peak RSS above the starting RSS."""

    @staticmethod
    def _kb(field: str) -> int:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
        raise KeyError(field)

    def __enter__(self):
        Path("/proc/self/clear_refs").write_text("5")  # resets VmHWM to the current RSS
        self.rss0, self.cpu0, self.t0 = self._kb("VmRSS"), time.process_time(), time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.s = time.perf_counter() - self.t0
        self.cpu = time.process_time() - self.cpu0
        self.peak_mb = round((self._kb("VmHWM") - self.rss0) / 1024, 1)

    def stats(self, prefix: str) -> dict:
        return {f"{prefix}_s": round(self.s, 3), f"{prefix}_cpu_s": round(self.cpu, 2),
                f"{prefix}_peak_mb": self.peak_mb}


def tree(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
    return len(files), sum(p.stat().st_size for p in files)


# -- compute ---------------------------------------------------------------------------------

def stage_compute(library: Path, n: int = 5000) -> None:
    from rdkit import Chem, RDLogger
    from rdkit.Chem import Descriptors

    from amdockvs.chemistry.descriptors import calculate_basic_descriptors, canonical_smiles, inchikey
    from amdockvs.chemistry.filtering import _pains_catalog
    from amdockvs.io.rows import evaluate_ligand_mol, structure_fields
    from amdockvs.molecules.fragments import ligand_fragments

    RDLogger.DisableLog("rdApp.*")
    raws = list(records(library, n))
    mols = [Chem.MolFromMolBlock(r) for r in raws]
    mols = [m for m in mols if m is not None]
    pains = _pains_catalog()

    def basic6(m):
        return (Descriptors.MolWt(m), Descriptors.MolLogP(m), Descriptors.NumHDonors(m),
                Descriptors.NumHAcceptors(m), Descriptors.TPSA(m), Descriptors.NumRotatableBonds(m))

    def today(m):
        decision, _ = evaluate_ligand_mol(mol=m, metadata={})
        return structure_fields(decision.kept_mol) if decision else None

    ops = {
        "parse_molblock": lambda i: Chem.MolFromMolBlock(raws[i]),
        "fragments": lambda i: ligand_fragments(mols[i]),
        "descriptors_6": lambda i: basic6(mols[i]),
        "descriptors_13": lambda i: calculate_basic_descriptors(mols[i]),
        "pains": lambda i: pains.GetMatches(mols[i]),
        "inchikey": lambda i: inchikey(mols[i]),
        "canonical_smiles": lambda i: canonical_smiles(mols[i]),
        "rows_import_today": lambda i: today(Chem.MolFromMolBlock(raws[i])),
    }
    for name, op in ops.items():
        mols = [Chem.MolFromMolBlock(r) for r in raws]  # fresh: RDKit caches ring info on the mol
        mols = [m for m in mols if m is not None]
        count = len(mols)
        start = time.perf_counter()
        for i in range(count):
            op(i)
        us = (time.perf_counter() - start) / count * 1e6
        emit("compute", name, us_per_mol=round(us, 1), n=count)


# -- app -------------------------------------------------------------------------------------

def db_breakdown(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sizes = {name: round(size / 1e6, 1) for name, size in con.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 15")}
        n_mol = con.execute("SELECT COUNT(*) FROM molecules").fetchone()[0]
        n_pay = con.execute("SELECT COUNT(*) FROM ms_payloads").fetchone()[0]
        n_stored = con.execute("SELECT COUNT(*) FROM molecules WHERE stored_payload_id IS NOT NULL").fetchone()[0]
        n_idx = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name='molecules'"
                            ).fetchone()[0]
    finally:
        con.close()
    return {"molecules": n_mol, "payload_rows": n_pay, "with_stored_payload": n_stored,
            "molecule_indexes": n_idx, "mb_by_object": sizes}


def stage_app(library: Path, work: Path, variant: str) -> None:
    """One import per process: a second runtime in the same process has no project flow."""
    from amdockvs import AMDockVSRuntime

    rt = AMDockVSRuntime()
    project = work / f"app_{variant}"
    try:
        rt.create_project(name=f"bench_{variant}", folder=project)
        start = time.monotonic()
        jobs = (rt.loader.load_ligands([library]) if variant == "rows_volumes"
                else rt.loader.shard_ligands([library]))
        rt.wait_for_jobs(jobs, timeout_s=6 * 3600)
        os.sync()
        elapsed = time.monotonic() - start
    finally:
        rt.close_project()
    files, size = tree(project)
    extra = db_breakdown(project / "project.db") if variant == "rows_volumes" else {}
    db_mb = round((project / "project.db").stat().st_size / 1e6, 1)
    emit("app", variant, durable_s=round(elapsed, 1), files=files, mb=round(size / 1e6, 1),
         project_db_mb=db_mb, **extra)
    shutil.rmtree(project, ignore_errors=True)


# -- storage ---------------------------------------------------------------------------------

def _prep(raw_batch: list[tuple[int, str]]) -> list[dict]:
    from rdkit import Chem, RDLogger
    from rdkit.Chem import Descriptors

    from amdockvs.chemistry.descriptors import calculate_basic_descriptors, canonical_smiles, inchikey

    RDLogger.DisableLog("rdApp.*")
    out = []
    for i, raw in raw_batch:
        m = Chem.MolFromMolBlock(raw)
        if m is None:
            continue
        d13 = calculate_basic_descriptors(m)
        out.append({"id": i, "name": raw.split("\n", 1)[0].strip() or f"m{i}", "sdf": raw,
                    "inchikey": inchikey(m), "canonical_smiles": canonical_smiles(m),
                    "n_atoms": m.GetNumAtoms(), **d13, "hba": int(Descriptors.NumHAcceptors(m))})
    return out


def prepare(library: Path) -> list[dict]:
    batch, batches = [], []
    for i, raw in enumerate(records(library), start=1):
        batch.append((i, raw))
        if len(batch) == 2000:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)
    with ProcessPoolExecutor() as pool:
        rows = [r for part in pool.map(_prep, batches) for r in part]
    for new_id, row in enumerate(rows, start=1):  # dense ids, like an import
        row["id"] = new_id
    return rows


MOLECULE_INDEXES = None  # filled from the model: name -> CREATE INDEX sql


def molecule_row(r: dict, payload_id: int, slim: bool) -> dict:
    now = datetime.now()
    base = {"id": r["id"], "name": r["name"], "current_payload_id": payload_id, "created_at": now, "updated_at": now,
            **{k: r[k] for k in DESCRIPTORS}}
    if slim:
        return base
    return {**base, "molecule_type": "small_molecule", "source": "/library.sdf", "source_index": r["id"],
            "input_format": "sdf", "is_ligand": True, "is_receptor": False,
            "inchikey": r["inchikey"], "canonical_smiles": r["canonical_smiles"], "n_atoms": r["n_atoms"],
            **{k: r[k] for k in ("fragment_count", "ring_count", "aromatic_ring_count", "hetero_atom_count",
                                 "heavy_atom_count", "formal_charge", "fraction_csp3")},
            "pains_matches": [], "ro5_violations": [], "extra_data": {"fragmentation": {"n": 1}},
            "usage_class": "general"}


def write_rows_volumes(rows: list[dict], work: Path, mode: str) -> tuple[Path, object]:
    """mode: indexes (today) | late (drop, load, rebuild) | slim (6 descriptors, no secondary index)."""
    from sqlalchemy import insert, text
    from sqlmodel import Session

    from amdockvs import AMDockVSRuntime
    from amdockvs.core.payloads import payload_store, structure_payload
    from amdockvs.models import MoleculeRecord

    rt = AMDockVSRuntime()
    project = work / f"st_rows_{mode}"
    rt.create_project(name=f"st_{mode}", folder=project)
    store_db = rt.workspace.project_db
    payloads = payload_store(store_db)
    table = MoleculeRecord.__table__
    with store_db.engine.connect() as con:
        idx = {n: s for n, s in con.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='molecules' AND sql IS NOT NULL")}
    if mode in ("late", "slim"):
        with store_db.engine.begin() as con:
            for name in idx:
                con.exec_driver_sql(f'DROP INDEX "{name}"')
    with meter() as m:
        for lo in range(0, len(rows), 10_000):  # one transaction per 10k (the import flush)
            part = rows[lo:lo + 10_000]
            with Session(store_db.engine) as s:
                ids = payloads.put(s, [structure_payload(r["sdf"], "sdf") for r in part])
                s.execute(insert(table), [molecule_row(r, pid, mode == "slim") for r, pid in zip(part, ids)])
                s.commit()
        if mode == "late":
            with store_db.engine.begin() as con:
                for sql in idx.values():
                    con.exec_driver_sql(sql)
        with store_db.engine.begin() as con:
            con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        os.sync()
    files, size = tree(project)
    emit("storage", f"rows_volumes_{mode}", **m.stats("write"), files=files, mb=round(size / 1e6, 1),
         project_db_mb=round((project / "project.db").stat().st_size / 1e6, 1),
         volumes_mb=round(tree(project / "volumes")[1] / 1e6, 1), indexes=0 if mode == "slim" else len(idx))
    return project, rt


def read_rows_volumes(project: Path, rt, ids: list[int]) -> None:
    from amdockvs.core.payloads import payload_store

    store_db = rt.workspace.project_db
    payloads = payload_store(store_db)
    db = str(project / "project.db")
    cold([project])
    with meter() as r:
        con = sqlite3.connect(db)
        pids = [p for (p,) in con.execute(
            f"SELECT current_payload_id FROM molecules WHERE id IN ({','.join(map(str, ids))})")]
        got = payloads.get_many(pids)
    assert len(got) == len(ids)
    cold([project])
    with meter() as q:
        hits = con.execute(QUERY.format(t="molecules")).fetchall()
    cold([project])
    with meter() as st:
        all_pids = [p for (p,) in con.execute("SELECT current_payload_id FROM molecules ORDER BY id")]
        total = sum(len(b) for _pid, b in payloads.iter_bytes(all_pids))
    con.close()
    emit("storage", "rows_volumes_read", **r.stats("random1000"), **q.stats("query"), query_hits=len(hits),
         **st.stats("stream"), stream_mb=round(total / 1e6, 1))


def write_mshard(rows: list[dict], work: Path, per_shard: int) -> Path:
    from ms_flow.core.data.shard import PayloadKind, Serializer, ShardWriter

    out = work / f"st_mshard_{per_shard}"
    out.mkdir()
    with meter() as m:
        for shard_id, lo in enumerate(range(0, len(rows), per_shard)):
            part = rows[lo:lo + per_shard]
            with ShardWriter(out / f"shard_{shard_id:05d}.mshard", dataset_id=None, shard_id=shard_id,
                             base_id=part[0]["id"], kind=PayloadKind.SDF, serializer=Serializer.RAW,
                             slot_count=len(part)) as w:
                for r in part:
                    w.add(r["id"], r["sdf"].encode())
        os.sync()
    files, size = tree(out)
    emit("storage", f"mshard_{per_shard}", **m.stats("write"), files=files, mb=round(size / 1e6, 1),
         note="uncompressed (v1 writes Codec.NONE only); no descriptors inside")
    return out


def read_mshard(out: Path, per_shard: int, ids: list[int]) -> None:
    from ms_flow.core.data.shard import Shard

    paths = sorted(out.glob("*.mshard"))
    cold([out])
    with meter() as r:
        got, opened = 0, {}  # a reader keeps its shards open
        for i in ids:
            path = paths[(i - 1) // per_shard]
            sh = opened.get(path) or opened.setdefault(path, Shard.open(path))
            got += len(bytes(sh[i].view()))
    for sh in opened.values():
        sh.close()
    cold([out])
    with meter() as st:
        total = 0
        for p in paths:
            with Shard.open(p) as sh:
                total += sum(len(bytes(v)) for v in sh.iter_views())  # bytes() touches the pages
    emit("storage", f"mshard_{per_shard}_read", **r.stats("random1000"), query_s=None,
         **st.stats("stream"), stream_mb=round(total / 1e6, 1),
         note="query needs descriptors: recompute (see compute stage) or a sidecar")


def write_parquet(rows: list[dict], work: Path, writer: str, row_group: int | None) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = work / f"st_parquet_{writer}_{row_group or 'default'}"
    out.mkdir()
    cols = ["id", "name", *DESCRIPTORS, "sdf"]
    with meter() as m:  # includes building the in-memory table: a writer always holds a row group
        if writer == "pandas":
            import pandas as pd

            pd.DataFrame({c: [r[c] for r in rows] for c in cols}).to_parquet(out / "part-0.parquet",
                                                                               compression="zstd", index=False)
        else:
            table = pa.table({c: [r[c] for r in rows] for c in cols})
            pq.write_table(table, out / "part-0.parquet", compression="zstd", row_group_size=row_group)
            del table
        os.sync()
    files, size = tree(out)
    meta = pq.ParquetFile(out / "part-0.parquet").metadata
    emit("storage", out.name.removeprefix("st_"), **m.stats("write"), files=files,
         mb=round(size / 1e6, 1), row_groups=meta.num_row_groups)
    return out


def read_parquet(out: Path, ids: list[int], threads: int | None) -> None:
    import duckdb
    import pyarrow.dataset as ds

    glob = str(out / "*.parquet")
    con = duckdb.connect()
    if threads:
        con.execute(f"SET threads={threads}")
    tag = f"{out.name.removeprefix('st_')}_read_duckdb_t{threads or 'all'}"
    cold([out])
    with meter() as r:
        got = con.execute(f"SELECT id, sdf FROM read_parquet('{glob}') WHERE id IN ({','.join(map(str, ids))})"
                          ).fetchall()
    assert len(got) == len(ids)
    cold([out])
    with meter() as q:
        hits = con.execute(QUERY.format(t=f"read_parquet('{glob}')")).fetchall()
    cold([out])
    with meter() as st:
        total = 0
        for batch in ds.dataset(out, format="parquet").to_batches(columns=["sdf"]):
            total += sum(len(x) for x in batch.column(0).to_pylist())
    emit("storage", tag, **r.stats("random1000"), **q.stats("query"), query_hits=len(hits),
         **st.stats("stream"), stream_mb=round(total / 1e6, 1))
    if threads is not None:
        return con.close()
    cold([out])
    with meter() as q:
        t = ds.dataset(out, format="parquet").to_table(
            columns=["id", "mw"], filter=(ds.field("logp") < 3) & (ds.field("hbd") <= 2) & (ds.field("tpsa") < 90))
        t = t.sort_by("mw").slice(0, 1000)
    emit("storage", f"{out.name.removeprefix('st_')}_read_pyarrow", **q.stats("query"), query_hits=t.num_rows)
    con.close()


def stage_storage(library: Path, work: Path) -> None:
    start = time.monotonic()
    rows = prepare(library)
    emit("storage", "prepare_in_memory", s=round(time.monotonic() - start, 1), n=len(rows))
    ids = random.Random(7).sample(range(1, len(rows) + 1), 1000)
    for mode in ("indexes", "late", "slim"):
        project, rt = write_rows_volumes(rows, work, mode)
        if mode == "indexes":
            read_rows_volumes(project, rt, ids)
        shutil.rmtree(project, ignore_errors=True)
    for per_shard in (1000, 65535):
        out = write_mshard(rows, work, per_shard)
        read_mshard(out, per_shard, ids)
        shutil.rmtree(out, ignore_errors=True)
    for writer, row_group in (("pyarrow", None), ("pyarrow", 10_000), ("pandas", None)):
        out = write_parquet(rows, work, writer, row_group)
        for threads in (4, None):
            read_parquet(out, ids, threads)
        shutil.rmtree(out, ignore_errors=True)


# -- scale -----------------------------------------------------------------------------------

def stage_scale(library: Path, work: Path, n: int = 1_000_000) -> None:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import insert

    from amdockvs.models import MoleculeRecord

    base = prepare_descriptors(library)
    rows = []
    for k in range(n):
        r = dict(base[k % len(base)])
        r["id"] = k + 1
        r["name"] = f"{r['name']}_{k // len(base)}"
        r["inchikey"] = f"{r['inchikey']}{k // len(base)}"  # unique keys, random order: the real index cost
        rows.append(r)
    table = MoleculeRecord.__table__
    from amdockvs import AMDockVSRuntime

    for mode in ("indexes", "late", "slim"):
        rt = AMDockVSRuntime()
        project = work / f"scale_{mode}"
        rt.create_project(name=f"scale_{mode}", folder=project)  # the app's own schema and pragmas
        db = project / "project.db"
        engine = rt.workspace.project_db.engine
        with engine.connect() as con:
            idx = {nm: s for nm, s in con.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='molecules' AND sql IS NOT NULL")}
        if mode != "indexes":
            with engine.begin() as con:
                for nm in idx:
                    con.exec_driver_sql(f'DROP INDEX "{nm}"')
        with meter() as w:
            for lo in range(0, n, 10_000):
                with engine.begin() as con:
                    con.execute(insert(table), [molecule_row(r, None, mode == "slim") for r in rows[lo:lo + 10_000]])
            if mode == "late":
                with engine.begin() as con:
                    for sql in idx.values():
                        con.exec_driver_sql(sql)
            with engine.begin() as con:
                con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            engine.dispose()
            os.sync()
        cold([db])
        with meter() as q:
            con = sqlite3.connect(db)
            hits = con.execute(QUERY.format(t="molecules")).fetchall()
            con.close()
        emit("scale", f"sqlite_{mode}", n=n, **w.stats("insert"), mb=round(db.stat().st_size / 1e6, 1),
             **q.stats("query"), query_hits=len(hits))
        shutil.rmtree(project, ignore_errors=True)
    pq_path = work / "scale.parquet"
    cols = ["id", "name", *DESCRIPTORS]
    with meter() as w:
        pq.write_table(pa.table({c: [r[c] for r in rows] for c in cols}), pq_path, compression="zstd")
        os.sync()
    for threads in (1, 4, None):
        con = duckdb.connect()
        if threads:
            con.execute(f"SET threads={threads}")
        cold([pq_path])
        with meter() as q:
            hits = con.execute(QUERY.format(t=f"read_parquet('{pq_path}')")).fetchall()
        emit("scale", f"duckdb_parquet_t{threads or 'all'}", n=n, **w.stats("write"),
             mb=round(pq_path.stat().st_size / 1e6, 1), **q.stats("query"), query_hits=len(hits))
        con.close()
    pq_path.unlink()


# -- point reads at scale --------------------------------------------------------------------

def stage_point(library: Path, work: Path, copies: int = 40) -> None:
    """The library x`copies` (~4M, new ids): what a Parquet-only store costs next to volumes when the
    UI opens one molecule, a job takes 1000 hits, and many small appends pile up as files."""
    import itertools

    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    base = prepare(library)
    n = len(base) * copies
    cols = ["id", "name", *DESCRIPTORS, "sdf"]
    schema = pa.schema([("id", pa.int64()), ("name", pa.string()), *[(k, pa.float64()) for k in DESCRIPTORS],
                        ("sdf", pa.string())])

    def stream():  # dense ids 1..n, so a row group is (id - 1) // row_group: a manifest's lookup
        for c in range(copies):
            for r in base:
                yield {**r, "id": c * len(base) + r["id"], "name": f"{r['name']}_{c}"}

    rnd = random.Random(7)
    ids = rnd.sample(range(1, n + 1), 1000)
    for row_group in (10_000, 1_000):
        path = work / f"point_{row_group}.parquet"
        it = stream()
        with meter() as w:
            with pq.ParquetWriter(path, schema, compression="zstd", write_page_index=True) as writer:
                while chunk := list(itertools.islice(it, row_group)):
                    writer.write_table(pa.table({c: [r[c] for r in chunk] for c in cols}, schema=schema),
                                       row_group_size=row_group)
            os.sync()
        tag = f"parquet_rg{row_group}"
        meta = pq.ParquetFile(path).metadata
        emit("point", tag, n=n, **w.stats("write"), mb=round(path.stat().st_size / 1e6, 1),
             row_groups=meta.num_row_groups, footer_kb=round(meta.serialized_size / 1024, 1))

        con = duckdb.connect()
        con.execute("SET threads=4")
        cold([path])
        with meter() as o:
            got = con.execute(f"SELECT sdf FROM read_parquet('{path}') WHERE id = {ids[0]}").fetchall()
        assert len(got) == 1
        cold([path])
        with meter() as r:
            got = con.execute(f"SELECT id FROM read_parquet('{path}') WHERE id IN ({','.join(map(str, ids))})"
                              " AND length(sdf) > 0").fetchall()
        assert len(got) == len(ids)
        cold([path])
        with meter() as q:
            hits = con.execute(QUERY.format(t=f"read_parquet('{path}')")).fetchall()
        con.close()
        emit("point", f"{tag}_duckdb_t4", **o.stats("open1"), **r.stats("random1000"),
             **q.stats("query"), query_hits=len(hits))

        # A manifest (SQLite) says which row group holds the id; read only that row group's sdf column.
        cold([path])
        with meter() as o:
            pf = pq.ParquetFile(path)
            g = (ids[0] - 1) // row_group
            sdf = pf.read_row_group(g, columns=["sdf"]).column(0)[(ids[0] - 1) % row_group].as_py()
        assert sdf
        cold([path])  # the reader stays open (footer parsed), the data is cold: the UI's next click
        with meter() as o2:
            g = (ids[1] - 1) // row_group
            sdf = pf.read_row_group(g, columns=["sdf"]).column(0)[(ids[1] - 1) % row_group].as_py()
        cold([path])
        with meter() as r:
            by_group: dict[int, list[int]] = {}
            for i in ids:
                by_group.setdefault((i - 1) // row_group, []).append((i - 1) % row_group)
            got = 0
            for g, offs in by_group.items():
                col = pf.read_row_group(g, columns=["sdf"]).column(0)
                got += sum(len(col[k].as_py()) for k in offs)
        emit("point", f"{tag}_manifest", **o.stats("open1"), **o2.stats("open1_reader_open"),
             **r.stats("random1000"), row_groups_touched=len(by_group))
        path.unlink()

    # Volumes: the same 4M SDF payloads (no molecule rows), the same reads.
    from sqlmodel import Session

    from amdockvs import AMDockVSRuntime
    from amdockvs.core.payloads import payload_store, structure_payload

    rt = AMDockVSRuntime()
    project = work / "point_volumes"
    rt.create_project(name="point_volumes", folder=project)
    store_db = rt.workspace.project_db
    payloads = payload_store(store_db)
    pids: list[int] = []
    it = stream()
    with meter() as w:
        while chunk := list(itertools.islice(it, 10_000)):
            with Session(store_db.engine) as s:
                pids += payloads.put(s, [structure_payload(r["sdf"], "sdf") for r in chunk])
                s.commit()
        with store_db.engine.begin() as con:
            con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        os.sync()
    files, size = tree(project)
    emit("point", "volumes", n=n, **w.stats("write"), mb=round(size / 1e6, 1), files=files,
         project_db_mb=round((project / "project.db").stat().st_size / 1e6, 1))
    want = [pids[i - 1] for i in ids]
    payloads = payload_store(store_db)  # fresh store: no cached volume handles
    cold([project])
    with meter() as o:
        assert payloads.get(want[0])
    cold([project])
    with meter() as o2:
        assert payloads.get(want[1])
    cold([project])
    with meter() as r:
        got = payloads.get_many(want)
    assert len(got) == len(want)
    emit("point", "volumes_read", **o.stats("open1"), **o2.stats("open1_store_open"), **r.stats("random1000"))
    rt.close_project()
    shutil.rmtree(project, ignore_errors=True)

    # Appends as Parquet files: 1000 jobs of 100 molecules each, then the same reads over the glob.
    small = work / "point_small"
    small.mkdir()
    with meter() as w:
        for k in range(1000):
            part = base[k * 100:(k + 1) * 100]
            pq.write_table(pa.table({c: [r[c] for r in part] for c in cols}), small / f"part-{k:05d}.parquet",
                           compression="zstd")
        os.sync()
    glob = str(small / "*.parquet")
    con = duckdb.connect()
    con.execute("SET threads=4")
    cold([small])
    with meter() as q:
        hits = con.execute(QUERY.format(t=f"read_parquet('{glob}')")).fetchall()
    cold([small])
    with meter() as o:
        got = con.execute(f"SELECT sdf FROM read_parquet('{glob}') WHERE id = {base[54_321]['id']}").fetchall()
    assert len(got) == 1
    cold([small])
    with meter() as cm:  # compaction: the 1000 files into one
        con.execute(f"COPY (SELECT * FROM read_parquet('{glob}')) TO '{work / 'compacted.parquet'}' "
                    "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 10000)")
        os.sync()
    con.close()
    emit("point", "parquet_1000_small_files", n=len(base), **w.stats("write"), mb=round(tree(small)[1] / 1e6, 1),
         **q.stats("query"), query_hits=len(hits), **o.stats("open1"), **cm.stats("compact"),
         compacted_mb=round((work / "compacted.parquet").stat().st_size / 1e6, 1))
    shutil.rmtree(small, ignore_errors=True)


def prepare_descriptors(library: Path) -> list[dict]:
    rows = prepare(library)
    for r in rows:
        r.pop("sdf")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path, nargs="?", default=LIBRARY)
    ap.add_argument("--stages", default="compute")
    ap.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()))
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="bench_storage_", dir=args.workdir))
    os.environ["HOME"] = str(work / "home")  # projects register here, not in the real ~/.molsuite
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    stages = {"compute": lambda: stage_compute(args.library),
              "app_rows": lambda: stage_app(args.library.resolve(), work, "rows_volumes"),
              "app_mshard": lambda: stage_app(args.library.resolve(), work, "mshard"),
              "storage": lambda: stage_storage(args.library.resolve(), work),
              "scale": lambda: stage_scale(args.library.resolve(), work),
              "point": lambda: stage_point(args.library.resolve(), work)}
    try:
        for name in args.stages.split(","):
            stages[name]()
    finally:
        out = REPO / "test" / "benchmarks" / "results" / f"storage_formats_{date.today()}.jsonl"
        with open(out, "a", encoding="utf-8") as fh:
            for row in RESULTS:
                fh.write(json.dumps(row) + "\n")
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
