"""The molecules table at ~1M distinct molecules, three ways, through the real SmartTableView.

    python test/benchmarks/bench_table_backends.py SMI_DIR [SMI_DIR ...] --work DIR [--variants A,B,C]

* A — today: `molecules` rows in SQLite (real schema + indexes) + payloads in volumes.
* B — the pilot: the same rows + payloads in Parquet segments (`ms_flow.parquet_payloads`).
* C — Parquet only: every column and the payload in Parquet segments, no SQLite at all; the
      table reads them through DuckDB (`ParquetQueryBuilder`, a QueryBuilder subclass).
* D — one DuckDB file (`molecules` table, PK on id, payload as a BLOB column).
* E — DuckLake: SQLite catalog + Parquet data files, through DuckDB.
* F — a Lance dataset (BTREE index on id once compacted); the table reads it through DuckDB.

C-F share the columns and the DuckDB-SQL builder. The `update` stage flips `excluded` on 10k rows
in one transaction and on 100 one by one, as the QSAR filter and the user do today.

Each SMI_DIR holds `.smi` files (`smiles zinc_id`, one header line). Molecules are prepared once
(the import's descriptors, inchikey, canonical SMILES) into `<work>/prepared.parquet`. Each
(variant, stage) runs in its own process under a throwaway HOME, so RSS is the table's own.
The view runs offscreen with the ligand table's page size (20) and default filters; the 2D
preview is replayed as one payload `get` per row of each loaded page, as `MoleculePreviews` does.
Prints one JSON line per measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_storage_formats import cold, meter, tree  # noqa: E402

BATCH = 10_000  # the import flush
PAGE = 20  # the ligand table's page_size
SOURCE = "/library/lead-like.smi"
COUNTS = ("fragment_count", "ring_count", "aromatic_ring_count", "hetero_atom_count", "heavy_atom_count",
          "formal_charge")
FIELDS = ("mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds", *COUNTS, "fraction_csp3")
COLUMNS = ("id", "name", "usage_class", "source", "input_format", "mw", "logp", "hbd", "hba", "tpsa",
           "rotatable_bonds", "inchikey")
LEAD_LIKE = [("logp", "<", 3.0), ("hbd", "<=", 2), ("tpsa", "<", 90.0)]


def emit(variant: str, **values) -> None:
    print(json.dumps({"variant": variant, **values}), flush=True)


def rss_mb() -> float:
    return round(meter._kb("VmRSS") / 1024, 1)


# -- prepare --------------------------------------------------------------------------------

def _prep(lines: list[str]) -> dict:
    from rdkit import Chem, RDLogger

    from amdockvs.chemistry.descriptors import calculate_basic_descriptors, canonical_smiles, inchikey

    RDLogger.DisableLog("rdApp.*")
    cols: dict[str, list] = {k: [] for k in ("name", "smiles", "inchikey", "canonical_smiles", "n_atoms", *FIELDS)}
    for line in lines:
        smiles, _, zid = line.strip().partition(" ")
        m = Chem.MolFromSmiles(smiles)
        if m is None:
            continue
        d = calculate_basic_descriptors(m)
        for k, v in (("name", f"ZINC{zid}"), ("smiles", smiles), ("inchikey", inchikey(m)),
                     ("canonical_smiles", canonical_smiles(m)), ("n_atoms", m.GetNumAtoms())):
            cols[k].append(v)
        for k in FIELDS:
            cols[k].append(d[k])
    return cols


def prepare(dirs: list[Path], out: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    def chunks():
        for d in dirs:
            for f in sorted(d.glob("*.smi")):
                with open(f) as fh:
                    next(fh)  # header
                    buf = []
                    for line in fh:
                        buf.append(line)
                        if len(buf) == 5000:
                            yield buf
                            buf = []
                    if buf:
                        yield buf

    n, writer = 0, None
    with meter() as m, ProcessPoolExecutor() as pool:
        for cols in pool.map(_prep, chunks()):
            k = len(cols["name"])
            table = pa.table({"id": list(range(n + 1, n + k + 1)), **cols})
            writer = writer or pq.ParquetWriter(out, table.schema, compression="zstd")
            writer.write_table(table)
            n += k
    writer.close()
    emit("prepare", n=n, **m.stats("prepare"), mb=round(out.stat().st_size / 1e6, 1))


def prepared_batches(work: Path):
    import pyarrow.parquet as pq

    yield from (b.to_pylist() for b in pq.ParquetFile(work / "prepared.parquet").iter_batches(BATCH))


# -- write ----------------------------------------------------------------------------------

def molecule_row(r: dict, payload_id: int, now: datetime) -> dict:
    return {"id": r["id"], "name": r["name"], "current_payload_id": payload_id, "stored_payload_id": payload_id,
            "created_at": now, "updated_at": now, "molecule_type": "small_molecule", "source": SOURCE,
            "source_index": r["id"], "input_format": "smi", "is_ligand": True, "is_receptor": False,
            "inchikey": r["inchikey"], "canonical_smiles": r["canonical_smiles"], "n_atoms": r["n_atoms"],
            **{k: r[k] for k in FIELDS}, "pains_matches": [], "ro5_violations": [], "extra_data": {},
            "usage_class": "general"}


def write_rows(variant: str, work: Path) -> None:
    """A and B: the rows sink's transaction, one per 10k, payloads through each store."""
    from sqlalchemy import event, insert
    from sqlmodel import Session

    from amdockvs import AMDockVSRuntime
    from amdockvs.core.payloads import payload_store, structure_payload
    from amdockvs.models import MoleculeRecord
    from ms_flow import parquet_payloads as PQ

    root = work / variant
    rt = AMDockVSRuntime()
    rt.create_project(name=f"tb_{variant}", folder=root)
    db = rt.workspace.project_db
    if variant == "B":
        PQ.ROW_GROUP_BYTES = 256 << 10  # the pilot's recommendation
        # ponytail: molecules.*_payload_id references ms_payloads; the pilot keeps its own
        # manifest, so the FK is switched off here (a real B moves the FK to ms_pq_payloads).
        event.listen(db.engine, "connect", lambda con, _: con.execute("PRAGMA foreign_keys=OFF"))
        db.engine.dispose()
        store = PQ.ParquetPayloadStore(db)
    else:
        store = payload_store(db)
    table, n = MoleculeRecord.__table__, 0
    with meter() as m:
        for part in prepared_batches(work):
            now = datetime.now()
            with Session(db.engine) as s:
                ids = store.put(s, [structure_payload(r["smiles"], "smi") for r in part])
                s.execute(insert(table), [molecule_row(r, pid, now) for r, pid in zip(part, ids)])
                s.commit()
            n += len(part)
        with db.engine.begin() as con:
            con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        os.sync()
    emit(variant, n=n, **m.stats("write"), project_db_mb=round((root / "project.db").stat().st_size / 1e6, 1),
         payloads_mb=round(tree(pdir)[1] / 1e6, 1) if (pdir := root / ("segments" if variant == "B" else "volumes")).exists() else 0,
         files=tree(root)[0])


C_SCHEMA = None


def c_schema():
    import pyarrow as pa

    global C_SCHEMA
    C_SCHEMA = C_SCHEMA or pa.schema(
        [("id", pa.int64()), ("name", pa.string()), ("molecule_type", pa.string()), ("source", pa.string()),
         ("source_index", pa.int64()), ("input_format", pa.string()), ("is_ligand", pa.bool_()),
         ("is_receptor", pa.bool_()), ("excluded", pa.bool_()), ("usage_class", pa.string()),
         ("inchikey", pa.string()), ("canonical_smiles", pa.string()), ("n_atoms", pa.int32()),
         *[(k, pa.float64() if k in ("mw", "logp", "tpsa", "fraction_csp3") else pa.int32()) for k in FIELDS],
         ("created_at", pa.timestamp("us")), ("kind", pa.string()), ("data", pa.large_binary())])
    return C_SCHEMA


def c_table(rows: list[dict], now: datetime):
    """One batch as an Arrow table with every column of variant C (and of D, E, F)."""
    import pyarrow as pa

    cols = {"id": [r["id"] for r in rows], "name": [r["name"] for r in rows],
            "molecule_type": ["small_molecule"] * len(rows), "source": [SOURCE] * len(rows),
            "source_index": [r["id"] for r in rows], "input_format": ["smi"] * len(rows),
            "is_ligand": [True] * len(rows), "is_receptor": [False] * len(rows), "excluded": [False] * len(rows),
            "usage_class": ["general"] * len(rows),
            **{k: [r[k] for r in rows] for k in ("inchikey", "canonical_smiles", "n_atoms", *FIELDS)},
            "created_at": [now] * len(rows), "kind": ["smi"] * len(rows),
            "data": [r["smiles"].encode() for r in rows]}
    return pa.table(cols, schema=c_schema())


def c_segment(path: Path, rows: list[dict], now: datetime) -> None:
    """One Parquet file, published the way the pilot publishes a segment: .part, fsync, rename."""
    import pyarrow.parquet as pq

    tmp = path.with_suffix(".part")
    pq.write_table(c_table(rows, now), tmp, compression="zstd", compression_level=3,
                   row_group_size=BATCH, write_statistics=True)
    with open(tmp, "rb+") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_parquet_only(work: Path) -> None:
    seg = work / "C" / "segments"
    seg.mkdir(parents=True)
    n = i = 0
    with meter() as m:
        for part in prepared_batches(work):
            i += 1
            c_segment(seg / f"{i:09d}.parquet", part, datetime.now())
            n += len(part)
        os.sync()
    emit("C", n=n, **m.stats("write"), segments=i, segments_mb=round(tree(seg)[1] / 1e6, 1))


def lake(root: Path, read_only: bool = False):
    import duckdb

    con = duckdb.connect()
    ro = ", READ_ONLY" if read_only else ""
    con.execute(f"ATTACH 'ducklake:sqlite:{root}/catalog.sqlite' AS lk (DATA_PATH '{root}/data/'{ro})")
    return con


def write_engine(variant: str, work: Path) -> None:
    """D: one DuckDB file. E: DuckLake (SQLite catalog, Parquet files). F: a Lance dataset.
    The same 10k batches as A-C, one transaction (D, E) or one commit (F) each."""
    import duckdb

    root = work / variant
    root.mkdir(parents=True)
    n = 0
    with meter() as m:
        if variant == "D":
            con = duckdb.connect(str(root / "project.duckdb"))
            con.execute("SET threads=4")
        elif variant == "E":
            con = lake(root)
        for part in prepared_batches(work):
            batch = c_table(part, datetime.now())
            if variant == "F":
                import lance

                lance.write_dataset(batch, root / "molecules.lance", mode="append" if n else "create")
            else:
                name = "molecules" if variant == "D" else "lk.molecules"
                if not n:
                    con.execute(f"CREATE TABLE {name} AS SELECT * FROM batch LIMIT 0")
                    if variant == "D":
                        con.execute("ALTER TABLE molecules ADD PRIMARY KEY (id)")
                con.execute("BEGIN")
                con.execute(f"INSERT INTO {name} SELECT * FROM batch")
                con.execute("COMMIT")
            n += len(part)
        if variant == "D":
            con.execute("CHECKPOINT")
        os.sync()
    emit(variant, n=n, **m.stats("write"), mb=round(tree(root)[1] / 1e6, 1), files=tree(root)[0])


def compact(variant: str, work: Path) -> None:
    root = work / variant
    if variant in ("E", "F"):
        files0 = tree(root)[0]
        with meter() as m:
            if variant == "E":
                con = lake(root)
                con.execute("CALL ducklake_merge_adjacent_files('lk')")
                con.execute("CALL ducklake_expire_snapshots('lk', older_than => now())")
                con.execute("CALL ducklake_cleanup_old_files('lk', cleanup_all => true)")
            else:
                import lance

                ds = lance.dataset(root / "molecules.lance")
                ds.optimize.compact_files(target_rows_per_fragment=1 << 20)
                ds.create_scalar_index("id", "BTREE")
                ds.cleanup_old_versions(older_than=__import__("datetime").timedelta(0))
            os.sync()
        emit(variant, **m.stats("compact"), files_before=files0, files_after=tree(root)[0],
             mb=round(tree(root)[1] / 1e6, 1))
        return
    if variant == "B":
        from ms_flow import parquet_payloads as PQ
        from ms_flow.core.database import ProjectStore

        PQ.ROW_GROUP_BYTES = 256 << 10
        project = ProjectStore.open_at(work / "B" / "project.db")
        store = PQ.ParquetPayloadStore(project)
        with meter() as m:
            stats = store.compact()
        emit("B", **m.stats("compact"), **stats, segments_mb=round(tree(store.directory)[1] / 1e6, 1))
        return
    import pyarrow.parquet as pq

    seg = work / "C" / "segments"
    old = sorted(seg.glob("*.parquet"))
    out = seg / f"{int(old[-1].stem) + 1:09d}.parquet"
    with meter() as m:  # ponytail: one file, row groups of 10k rows; split by size past a few GB
        with pq.ParquetWriter(out.with_suffix(".part"), c_schema(), compression="zstd", compression_level=3) as w:
            for f in old:
                w.write_table(pq.read_table(f), row_group_size=BATCH)
        with open(out.with_suffix(".part"), "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(out.with_suffix(".part"), out)
        for f in old:
            f.unlink()
    emit("C", **m.stats("compact"), segments_before=len(old), segments_after=1,
         segments_mb=round(tree(seg)[1] / 1e6, 1))


# -- the table over Parquet ------------------------------------------------------------------

class ParquetSource:
    """What variants C-F hand the view in place of a SessionProvider: a DuckDB connection, and
    the table expression `{src}` stands for (Parquet glob, DuckDB table, DuckLake table, Lance)."""

    def __init__(self, variant: str, root: Path, threads: int = 4, memory: str = "512MB"):
        import duckdb

        self.ds = None
        if variant == "C":
            self.con, self.src = duckdb.connect(), f"read_parquet('{root / 'segments' / '*.parquet'}')"
        elif variant == "D":
            self.con, self.src = duckdb.connect(str(root / "project.duckdb"), read_only=True), "molecules"
        elif variant == "E":
            self.con, self.src = lake(root, read_only=True), "lk.molecules"
        else:
            import lance

            self.ds = lance.dataset(root / "molecules.lance")
            self.con, self.src = duckdb.connect(), "lance_ds"
        self.con.execute(f"SET threads={threads}; SET memory_limit='{memory}'")
        try:
            self.con.execute("SET parquet_metadata_cache=true")
        except Exception:
            pass

    def rows(self, sql: str, params=()) -> tuple[list[str], list[tuple]]:
        cur = self.con.cursor()
        if self.ds is not None:  # a registered view lives on its own cursor
            cur.register("lance_ds", self.ds)
        cur.execute(sql.replace("{src}", self.src), list(params))
        return [d[0] for d in cur.description], cur.fetchall()

    def get(self, mol_id: int) -> bytes:
        if self.ds is not None:  # Lance's own point lookup (BTREE index on id once compacted)
            return self.ds.to_table(columns=["data"], filter=f"id = {int(mol_id)}")["data"][0].as_py()
        return self.rows("SELECT data FROM {src} WHERE id = ?", [mol_id])[1][0][0]

    def get_session(self):  # the base QueryBuilder must never reach SQL through this
        raise NotImplementedError


def _filter_sql(f) -> tuple[str, list]:
    col, op = f'"{f.field}"', f.op.value
    if op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {op}", []
    if op in ("IN", "NOT IN"):
        return f"{'NOT ' if op == 'NOT IN' else ''}list_contains(?, {col})", [list(f.value)]
    return f"{col} {op} ?", [f.value]


class ParquetQueryBuilder:
    """`QueryBuilder` whose queries are DuckDB SQL over the segments (keyset, OFFSET, COUNT).

    Rows come back as namespaces in `__raw__`, so the view's cursor, selection and `getattr(raw,
    "id")` callers work unchanged. Same NULL order as SQLite: first ascending, last descending.
    """

    def __new__(cls, db, config):
        from ms_components.ms_table.query_builder import QueryBuilder

        if not isinstance(db, ParquetSource):
            return QueryBuilder(db, config)
        sub = type("_ParquetQueryBuilder", (cls._Impl, QueryBuilder), {})
        return sub(db, config)

    class _Impl:
        def _cols(self) -> str:
            fields = dict.fromkeys(["id", *(c.field for c in self._config.columns if "." not in c.field)])
            return ", ".join(f'"{f}"' for f in fields)

        def _where(self, cursor=None) -> tuple[str, list]:
            parts, params = [], []
            for f in self._all_filters():
                sql, p = _filter_sql(f)
                parts.append(sql)
                params += p
            if cursor is not None:
                value, pk = cursor
                if not self._sorts:
                    parts.append("id > ?")
                    params.append(pk)
                else:
                    s, c = self._sorts[0], f'"{self._sorts[0].field}"'
                    if value is None:
                        parts.append(f"({c} IS NULL AND id > ?)" if s.descending
                                     else f"({c} IS NOT NULL OR ({c} IS NULL AND id > ?))")
                        params.append(pk)
                    elif s.descending:
                        parts.append(f"({c} < ? OR ({c} = ? AND id > ?) OR {c} IS NULL)")
                        params += [value, value, pk]
                    else:
                        parts.append(f"({c} > ? OR ({c} = ? AND id > ?))")
                        params += [value, value, pk]
            return (" WHERE " + " AND ".join(parts)) if parts else "", params

        def _order(self) -> str:
            if not self._sorts:
                return " ORDER BY id"
            s = self._sorts[0]
            return f' ORDER BY "{s.field}" {"DESC NULLS LAST" if s.descending else "ASC NULLS FIRST"}, id'

        def _select(self, cursor=None, limit=None, offset=0) -> list[dict]:
            where, params = self._where(cursor)
            sql = f"SELECT {self._cols()} FROM {{src}}{where}{self._order()} LIMIT {int(limit)} OFFSET {int(offset)}"
            names, rows = self._db.rows(sql, params)
            return [self._row_to_dict(SimpleNamespace(**dict(zip(names, r)))) for r in rows]

        def count(self) -> int:
            where, params = self._where()
            return int(self._db.rows(f"SELECT count(*) FROM {{src}}{where}", params)[1][0][0])

        def all_ids(self) -> list[int]:
            where, params = self._where()
            return [r[0] for r in self._db.rows(f"SELECT id FROM {{src}}{where}{self._order()}", params)[1]]

        def fetch_window_after(self, cursor):
            rows = self._select(cursor, self._page_size + 1)
            return rows[:self._page_size], len(rows) > self._page_size

        def fetch_after(self, cursor):
            return self._select(cursor, self._page_size), self.count()

        def fetch_window_at(self, page_index: int):
            rows = self._select(None, self._page_size + 1, max(0, int(page_index)) * self._page_size)
            return rows[:self._page_size], len(rows) > self._page_size

        def fetch(self):
            return self._select(None, self._page_size, (self._page - 1) * self._page_size), self.count()


# -- the table ------------------------------------------------------------------------------

def table_config():
    from amdockvs.models import MoleculeRecord
    from ms_components.ms_table import ColumnDef, FilterOperator, FilterSpec, TableConfig, TableLoadMode

    return TableConfig(
        model_class=MoleculeRecord,
        columns=[ColumnDef(f, label=f, sortable=True, filterable=True) for f in COLUMNS],
        default_filters=[FilterSpec("is_ligand", FilterOperator.EQ, True, label="is_ligand"),
                         FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only")],
        page_size=PAGE, load_mode=TableLoadMode.INFINITE)


def bench_table(variant: str, work: Path, label: str) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from ms_components.ms_table import FilterOperator, FilterSpec, SortSpec
    from ms_components.ms_table import smart_table_view as stv

    app = QApplication.instance() or QApplication([])
    root = work / variant
    if variant in "CDEF":
        db = ParquetSource(variant, root)
        get = db.get
        pid = "id"
    else:
        from ms_flow import parquet_payloads as PQ
        from ms_flow.core.database import ProjectStore

        db = ProjectStore.open_at(root / "project.db")
        store = PQ.ParquetPayloadStore(db) if variant == "B" else db.payloads
        get = store.get
        pid = "current_payload_id"
    stv.QueryBuilder = ParquetQueryBuilder  # SmartTableView builds its builder in __init__
    rss0 = rss_mb()
    cold([root])

    def previews(view) -> float:
        """One payload per row of the page at the top of the viewport, as MoleculePreviews asks."""
        t = time.perf_counter()
        for row in view._infinite_pages.get(view._table.verticalScrollBar().value() // PAGE, []):
            get(int(getattr(row["__raw__"], pid)))
        return time.perf_counter() - t

    def top(view) -> None:
        view._table.verticalScrollBar().setValue(0)
        app.processEvents()

    def scroll(view, pages: int) -> dict:
        """The user drags down one page at a time; the view loads what the viewport needs."""
        bar, lat, prev, query = view._table.verticalScrollBar(), [], [], []
        for _ in range(pages):
            q0, t = len(queries), time.perf_counter()
            bar.setValue(bar.value() + PAGE)
            app.processEvents()
            lat.append(time.perf_counter() - t)
            query.append(sum(queries[q0:]))
            prev.append(previews(view))
        lat.sort()
        prev.sort()
        query.sort()
        return {"pages": pages, "query_p50_ms": round(query[len(query) // 2] * 1e3, 2),
                "query_p95_ms": round(query[int(len(query) * .95)] * 1e3, 2), "page_p50_ms": round(lat[len(lat) // 2] * 1e3, 2),
                "page_p95_ms": round(lat[int(len(lat) * .95)] * 1e3, 2), "page_max_ms": round(lat[-1] * 1e3, 1),
                "previews20_p50_ms": round(prev[len(prev) // 2] * 1e3, 2),
                "previews20_p95_ms": round(prev[int(len(prev) * .95)] * 1e3, 2),
                "loaded_rows": view._model.loaded_count, "rss_mb": rss_mb()}

    with meter() as m:
        view = stv.SmartTableView(db=db, config=table_config())
        view.resize(1200, 800)
        view.show()
        app.processEvents()
    total = view._model.total_items
    queries: list[float] = []  # seconds inside the builder: the backend's share of each step
    for name in ("fetch_window_after", "fetch_window_at", "count"):
        def timed(*args, _f=getattr(view._builder, name)):
            t = time.perf_counter()
            try:
                return _f(*args)
            finally:
                queries.append(time.perf_counter() - t)
        setattr(view._builder, name, timed)
    emit(label, step="open", total=total, **m.stats("open"), rss_mb=rss_mb(), rss0_mb=rss0)
    with meter() as m:
        out = scroll(view, 500)
    emit(label, step="scroll_10k_rows", **out, **m.stats("scroll"))
    q0 = len(queries)
    with meter() as m:
        view._table.verticalScrollBar().setValue(total // 2)
        app.processEvents()
    emit(label, step="jump_middle", **m.stats("jump"), query_ms=round(sum(queries[q0:]) * 1e3, 1), first_id=getattr(view._infinite_pages[min(
        view._infinite_pages)][0]["__raw__"], "id"))
    with meter() as m:
        out = scroll(view, 100)
    emit(label, step="scroll_after_jump", **out, **m.stats("scroll"))
    top(view)
    q0 = len(queries)
    with meter() as m:
        view._builder.set_sort([SortSpec("mw", descending=True)])
        view.refresh()
        app.processEvents()
    emit(label, step="sort_mw_desc", **m.stats("sort"), query_ms=round(sum(queries[q0:]) * 1e3, 1), top_mw=view._infinite_pages[0][0]["mw"])
    with meter() as m:
        out = scroll(view, 100)
    emit(label, step="scroll_sorted", **out, **m.stats("scroll"))
    top(view)
    q0 = len(queries)
    with meter() as m:
        view._builder.set_sort([])
        view.set_filters(list(view._config.default_filters) +
                         [FilterSpec(f, FilterOperator(op), v, label=f) for f, op, v in LEAD_LIKE])
        app.processEvents()
    emit(label, step="filter_lead_like", total=view._model.total_items, **m.stats("filter"), query_ms=round(sum(queries[q0:]) * 1e3, 1))
    with meter() as m:
        out = scroll(view, 100)
    emit(label, step="scroll_filtered", **out, **m.stats("scroll"))
    emit(label, step="end", rss_mb=rss_mb())
    view.deleteLater()
    app.processEvents()
    os._exit(0)  # ponytail: skip the PySide6 teardown segfault at exit


# -- updates ---------------------------------------------------------------------------------

def update(variant: str, work: Path) -> None:
    """Molecules are not write-once: exclude 10k at once (a filter applied), then 100 one at a time
    (the user's clicks), each its own transaction; then the table's count reads the result."""
    import random

    root = work / variant
    if variant == "C":
        emit("C", step="update", supported=False)
        return
    ids = random.Random(7).sample(range(1, 1_000_001), 10_100)
    bulk, single = ids[:10_000], ids[10_000:]
    if variant in "AB":
        import sqlite3

        from amdockvs.models import MoleculeRecord

        con, name = sqlite3.connect(root / "project.db", isolation_level=None), MoleculeRecord.__table__.name

        def run(sql, params):
            con.execute("BEGIN")
            con.execute(sql.replace("{t}", name), params)
            con.execute("COMMIT")

        def bulk_update():
            run(f"UPDATE {{t}} SET excluded = 1 WHERE id IN ({','.join('?' * len(bulk))})", bulk)

        def one(i):
            run("UPDATE {t} SET excluded = 1 WHERE id = ?", [i])

        def count():
            return con.execute(f"SELECT count(*) FROM {name} WHERE is_ligand AND NOT excluded").fetchone()[0]
    elif variant in "DE":
        import pyarrow as pa

        import duckdb

        con = duckdb.connect(str(root / "project.duckdb")) if variant == "D" else lake(root)
        name = "molecules" if variant == "D" else "lk.molecules"
        con.register("bulk_ids", pa.table({"id": pa.array(bulk, pa.int64())}))

        def bulk_update():
            con.execute("BEGIN")
            con.execute(f"UPDATE {name} SET excluded = true WHERE id IN (SELECT id FROM bulk_ids)")
            con.execute("COMMIT")

        def one(i):
            con.execute("BEGIN")
            con.execute(f"UPDATE {name} SET excluded = true WHERE id = ?", [i])
            con.execute("COMMIT")

        def count():
            return con.execute(f"SELECT count(*) FROM {name} WHERE is_ligand AND NOT excluded").fetchone()[0]
    else:
        import lance

        path = root / "molecules.lance"

        def bulk_update():
            lance.dataset(path).update({"excluded": "true"}, where=f"id IN ({','.join(map(str, bulk))})")

        def one(i):
            lance.dataset(path).update({"excluded": "true"}, where=f"id = {i}")

        def count():
            return lance.dataset(path).count_rows(filter="is_ligand AND NOT excluded")

    with meter() as m:
        bulk_update()
    emit(variant, step="update_bulk_10k", **m.stats("bulk"))
    lat = []
    for i in single:
        t = time.perf_counter()
        one(i)
        lat.append(time.perf_counter() - t)
    lat.sort()
    emit(variant, step="update_single_x100", p50_ms=round(lat[50] * 1e3, 2), p95_ms=round(lat[95] * 1e3, 2),
         total_s=round(sum(lat), 2))
    t = time.perf_counter()
    n = count()
    emit(variant, step="count_after", n=n, s=round(time.perf_counter() - t, 3),
         mb=round(tree(root)[1] / 1e6, 1), files=tree(root)[0])


# -- driver ---------------------------------------------------------------------------------

def run(stage: str, variant: str, work: Path, label: str = "") -> None:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    cmd = [sys.executable, __file__, "--work", str(work), "--stage", stage, "--variants", variant]
    if label:
        cmd += ["--label", label]
    subprocess.run(cmd, env=env, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", type=Path, nargs="*")
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--variants", default="A,B,C,D,E,F")
    ap.add_argument("--stage", default="all")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    v = a.variants
    if a.stage == "prepare":
        prepare(a.dirs, a.work / "prepared.parquet")
    elif a.stage == "write":
        write_parquet_only(a.work) if v == "C" else write_engine(v, a.work) if v in "DEF" else write_rows(v, a.work)
    elif a.stage == "compact":
        compact(v, a.work)
    elif a.stage == "update":
        update(v, a.work)
    elif a.stage == "table":
        bench_table(v, a.work, a.label or v)
    else:
        a.work.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = tempfile.mkdtemp(prefix="bench_home_")  # projects never reach the real registry
        if not (a.work / "prepared.parquet").exists():
            run("prepare", "", a.work) if not a.dirs else prepare(a.dirs, a.work / "prepared.parquet")
        for variant in v.split(","):
            run("write", variant, a.work)
            if variant in "BCEF":
                run("table", variant, a.work, f"{variant}_unsealed")
                run("compact", variant, a.work)
            run("table", variant, a.work)
            run("update", variant, a.work)
            shutil.rmtree(a.work / variant)
        shutil.rmtree(os.environ["HOME"], ignore_errors=True)


if __name__ == "__main__":
    main()
