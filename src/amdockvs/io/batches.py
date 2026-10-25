"""Import batches: one row per imported file, written when the import is submitted.

The job only carries the batch ids (`import_batch_ids` in its params), so a resumed job writes
into the same batches. `quick_hash` finds a file imported before without reading it whole.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select
from sqlmodel import col

from amdockvs.core.vocab import CODES, FileFormat, ImportKind
from amdockvs.io.parsers.readers import count_import_records
from amdockvs.io.formats import normalized_suffix
from amdockvs.models.molecules import ImportBatch, MoleculeRecord
from amdockvs.models.screening import ScreeningShard

_EDGE = 1 << 20  # bytes hashed at each end
_SMILES_SUFFIXES = {".smi", ".smiles", ".txt", ".csv", ".tsv"}


class DuplicateImportError(ValueError):
    """Every file named here was imported completely before; `force=True` imports them again."""

    def __init__(self, files: list[str]):
        super().__init__("Already imported: " + ", ".join(Path(f).name for f in files))
        self.files = files


def quick_hash(path: str | Path) -> str:
    """Size + first MB + last MB: ~2 ms whatever the size. Two files that differ only in the
    middle collide — `n_records` is checked as well before calling a file a duplicate."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.blake2b(str(size).encode(), digest_size=16)
    with path.open("rb") as handle:
        digest.update(handle.read(_EDGE))
        if size > _EDGE:
            handle.seek(max(_EDGE, size - _EDGE))
            digest.update(handle.read(_EDGE))
    return digest.hexdigest()


def file_format_of(path: str | Path) -> str:
    suffix = normalized_suffix(Path(path))
    name = FileFormat.SMILES if suffix in _SMILES_SUFFIXES else suffix.lstrip(".")
    return name if name in CODES["file_format"] else FileFormat.UNKNOWN


def previous_import(project_db, path: str | Path, digest: str) -> str:
    """"complete", "partial" or "" for a file whose hash a batch already has."""
    with project_db.get_session() as session:
        read = session.execute(
            select(func.max(ImportBatch.n_records)).where(ImportBatch.quick_hash == digest)
        ).scalar()
    if read is None:
        return ""
    return "complete" if int(read) >= count_import_records(path) else "partial"


def duplicate_imports(project_db, files: Iterable[str | Path]) -> list[str]:
    """The files (resolved) that a batch already imported completely."""
    resolved = [str(Path(f).expanduser().resolve()) for f in files]
    return [f for f in resolved if previous_import(project_db, f, quick_hash(f)) == "complete"]


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
    files = list(files)
    if not force and (done := duplicate_imports(project_db, files)):
        raise DuplicateImportError(done)
    digests = {str(Path(f).expanduser().resolve()): quick_hash(Path(f).expanduser()) for f in files}
    labels = {str(Path(k).expanduser().resolve()): str(v) for k, v in dict(labels or {}).items()}
    batches = {
        f: ImportBatch(source=labels.get(f) or f, quick_hash=d, format=file_format_of(f), kind=kind,
                       extra=dict(extra or {}))
        for f, d in digests.items()
    }
    with project_db.get_session() as session:
        session.add_all(batches.values())
        session.commit()
        return {f: int(b.id) for f, b in batches.items()}


def library_names(project_db) -> dict[int, str]:
    """batch id → the name its hits are called by (`<name>:<record id>`): the file stem."""
    with project_db.get_session() as session:
        rows = session.execute(select(ImportBatch.id, ImportBatch.source)).all()
    return {int(i): Path(str(source)).stem or "library" for i, source in rows}


def stamp_job(project_db, batch_ids: Iterable[int], job_id: str) -> None:
    ids = list(batch_ids)
    with project_db.get_session() as session:
        for batch in session.execute(select(ImportBatch).where(col(ImportBatch.id).in_(ids))).scalars():
            batch.job_id = str(job_id)
        session.commit()


def finish_batches(project_db, batch_ids: Mapping[str, int], *, completed: bool) -> None:
    """Count what each batch wrote. A completed job read the whole file; one that stopped read
    up to its last written record — a floor, which is all `previous_import` needs."""
    with project_db.get_session() as session:
        for path, batch_id in dict(batch_ids or {}).items():
            batch = session.get(ImportBatch, int(batch_id))
            if batch is None:
                continue
            if batch.kind == ImportKind.SHARDS:
                written = session.execute(select(func.coalesce(func.sum(ScreeningShard.n_records), 0)).where(
                    ScreeningShard.import_batch_id == batch.id)).scalar()
                last = None
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
    """A column of the row's import batch, as a correlated scalar subquery (catalog columns)."""
    return select(getattr(ImportBatch, name)).where(ImportBatch.id == model.import_batch_id).scalar_subquery()


__all__ = [
    "DuplicateImportError",
    "batch_field",
    "duplicate_imports",
    "file_format_of",
    "finish_batches",
    "library_names",
    "previous_import",
    "quick_hash",
    "register_batches",
    "stamp_job",
]
