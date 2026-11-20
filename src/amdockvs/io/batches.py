"""Import batches: one row per imported file, written when the import is submitted.

The job only carries the batch ids (`import_batch_ids` in its params), so a resumed job writes
into the same batches. `quick_hash` finds a file imported before without reading it whole.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import String, case, cast, func, insert, literal, or_, select, update
from sqlmodel import col

from amdockvs.core.normalize import resolved_path
from amdockvs.core.vocab import CODES, FileFormat, ImportKind
from amdockvs.io.parsers.readers import count_import_records
from amdockvs.io.formats import normalized_suffix
from amdockvs.models.molecules import ImportBatch, MoleculeRecord
from ms_flow.core.database.project_models import CatalogFile

_EDGE = 64 << 10  # bytes hashed at each end: a whole PDB fits in 1 MB, a proteome is 25k of them
_SMILES_SUFFIXES = {".smi", ".smiles", ".txt", ".csv", ".tsv"}


class DuplicateImportError(ValueError):
    """Every file named here was imported completely before; `force=True` imports them again."""

    def __init__(self, files: list[str]):
        super().__init__("Already imported: " + ", ".join(Path(f).name for f in files))
        self.files = files


def quick_hash(path: str | Path) -> str:
    """Size + first and last 64 KiB: constant time whatever the size. Two files that differ only in the
    middle collide — `n_records` is checked as well before calling a file a duplicate.
    Memoized on (path, size, mtime): one import asks for the same files more than once."""
    stat = Path(path).stat()
    return _quick_hash(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=1 << 16)
def _quick_hash(path: str, size: int, _mtime_ns: int) -> str:
    digest = hashlib.blake2b(str(size).encode(), digest_size=16)
    with open(path, "rb") as handle:
        digest.update(handle.read(_EDGE))
        if size > _EDGE:
            handle.seek(max(_EDGE, size - _EDGE))
            digest.update(handle.read(_EDGE))
    return digest.hexdigest()


def file_format_of(path: str | Path) -> str:
    suffix = normalized_suffix(Path(path))
    name = FileFormat.SMILES if suffix in _SMILES_SUFFIXES else suffix.lstrip(".")
    return name if name in CODES["file_format"] else FileFormat.UNKNOWN


def duplicate_imports(project_db, files: Iterable[str | Path]) -> list[str]:
    """Files imported completely before whose batches still contain their primary role or shards.

    Batch counters describe import history, so they survive deletion of the imported records.
    History alone must not prevent importing a deleted file again.
    """
    digests = {f: quick_hash(f) for f in map(resolved_path, files)}
    role = func.coalesce(ImportBatch.extra["role"].as_string(), "")
    read: dict[str, int] = {}
    with project_db.get_session() as session:
        for part in _chunks(list(set(digests.values()))):
            read.update(session.execute(
                select(ImportBatch.quick_hash, func.max(ImportBatch.n_records))
                .where(col(ImportBatch.quick_hash).in_(part))
                .where(or_(
                    select(MoleculeRecord.id)
                    .where(MoleculeRecord.import_batch_id == ImportBatch.id)
                    # A receptor batch also contains cocrystal reference ligands. Those
                    # alone do not mean the receptor is still present after deletion.
                    .where(or_(role != "receptor", MoleculeRecord.is_receptor.is_(True)))
                    .exists(),
                    select(CatalogFile.id).where(_holds_batch(ImportBatch.id)).exists(),
                ))
                .group_by(ImportBatch.quick_hash)
            ).all())
    return [f for f, d in digests.items() if d in read and int(read[d]) >= count_import_records(f)]


def _holds_batch(batch_id):
    """A catalog file with a shard of `batch_id`: its `scopes` list names it."""
    return (literal(",") + CatalogFile.scopes + literal(",")).like(
        literal("%,") + cast(batch_id, String) + literal(",%"))


def add_imported(project_db, written: Mapping[int | None, int]) -> None:
    """Add what a shard import wrote to each batch's `n_imported`: the queue counts it as it
    cuts, since no row per shard is kept to count it from afterwards."""
    with project_db.get_session() as session:
        for batch_id, n in dict(written or {}).items():
            if batch_id is not None and n:
                session.execute(update(ImportBatch).where(ImportBatch.id == int(batch_id))
                                .values(n_imported=ImportBatch.n_imported + int(n)))
        session.commit()


def _chunks(values: list, size: int = 30_000):
    """Pieces under SQLite's 32766 bound variables."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


def register_batches(
    project_db,
    files: Iterable[str | Path],
    *,
    kind: str = ImportKind.ROWS,
    labels: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """One batch per file, keyed by its resolved path. Raises `DuplicateImportError` when any
    file was imported completely before, unless `force`; a partial import is not refused."""
    files = [resolved_path(f) for f in files]
    if not force and (done := duplicate_imports(project_db, files)):
        raise DuplicateImportError(done)
    labels = {resolved_path(k): str(v) for k, v in dict(labels or {}).items()}
    # Core insert, not ORM objects: a proteome is 25k batches, ~20x faster this way.
    rows = [
        {"source": labels.get(f) or f, "quick_hash": quick_hash(f), "n_records": 0, "n_imported": 0,
         "format": file_format_of(f), "kind": kind, "job_id": "", "extra": dict(extra or {}),
         "created_at": datetime.now()}
        for f in files
    ]
    ids: list[int] = []
    with project_db.get_session() as session:
        for part in _chunks(rows, 5_000):
            ids += session.execute(
                insert(ImportBatch).returning(ImportBatch.id, sort_by_parameter_order=True), part
            ).scalars().all()
        session.commit()
    return dict(zip(files, map(int, ids)))


def library_names(project_db) -> dict[int, str]:
    """batch id → the name its hits are called by (`<name>:<record id>`): the file stem."""
    with project_db.get_session() as session:
        rows = session.execute(select(ImportBatch.id, ImportBatch.source)).all()
    return {int(i): Path(str(source)).stem or "library" for i, source in rows}


def stamp_job(project_db, batch_ids: Iterable[int], job_id: str) -> None:
    with project_db.get_session() as session:
        for part in _chunks(list(batch_ids)):
            session.execute(update(ImportBatch).where(col(ImportBatch.id).in_(part)).values(job_id=str(job_id)))
        session.commit()


def finish_batches(project_db, batch_ids: Mapping[str, int], *, completed: bool) -> None:
    """Count what each batch wrote. A completed job read the whole file; one that stopped read
    up to its last written record — a floor, which is all `duplicate_imports` needs."""
    with project_db.get_session() as session:
        for path, batch_id in dict(batch_ids or {}).items():
            batch = session.get(ImportBatch, int(batch_id))
            if batch is None:
                continue
            if batch.kind == ImportKind.SHARDS:
                written, last = batch.n_imported, None  # counted by the queue (`add_imported`)
            else:
                written, last = session.execute(
                    select(func.count(), func.max(MoleculeRecord.source_index))
                    .where(MoleculeRecord.import_batch_id == batch.id)
                ).one()
            batch.n_imported = int(written or 0)
            if completed and Path(path).is_file():
                batch.n_records = count_import_records(path)
            elif last is not None:
                batch.n_records = int(last) + 1
        session.commit()


def batch_field(model, name: str):
    """A column of the row's import batch, as a correlated scalar subquery (catalog columns).
    A catalog file holds the shards of many batches: its first one, and ``(+N)`` for the rest."""
    if model is not CatalogFile:
        return select(getattr(ImportBatch, name)).where(ImportBatch.id == model.import_batch_id).scalar_subquery()
    scopes = model.scopes
    listed = scopes + literal(",")
    first = cast(func.substr(listed, 1, func.instr(listed, ",") - 1), String)
    value = select(getattr(ImportBatch, name)).where(cast(ImportBatch.id, String) == first).scalar_subquery()
    if name != "source":
        return value
    more = func.length(scopes) - func.length(func.replace(scopes, ",", ""))
    return value + case((more > 0, literal(" (+") + cast(more, String) + literal(")")), else_=literal(""))


__all__ = [
    "DuplicateImportError",
    "add_imported",
    "batch_field",
    "duplicate_imports",
    "file_format_of",
    "finish_batches",
    "library_names",
    "quick_hash",
    "register_batches",
    "stamp_job",
]
