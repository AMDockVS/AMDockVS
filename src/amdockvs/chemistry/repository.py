from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import func, select

from ms_flow.core.database import ProjectStore
from ms_flow.query import QuerySpec, db_pages

from amdockvs.constants import RESOURCE_MOLECULES, TABLE_MOLECULES
from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.scopes import molecule_set_spec


def _resolve_role_storage_dir(config: Mapping[str, Any] | None, *, role: str) -> Path:
    resources = dict((config or {}).get("project_resources") or {})
    molecule_resource = dict(resources.get(RESOURCE_MOLECULES) or {})
    path_text = str(molecule_resource.get("path") or "").strip()
    if not path_text:
        raise ValueError("Missing project resource 'molecules' for chemistry job.")
    path = Path(path_text).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_ligand_storage_dir(config: Mapping[str, Any] | None) -> Path:
    return _resolve_role_storage_dir(config, role="ligand")


def resolve_receptor_storage_dir(config: Mapping[str, Any] | None) -> Path:
    return _resolve_role_storage_dir(config, role="receptor")


def project_db_path(project_db) -> Path:
    db_path = getattr(project_db, "db_path", None)
    if db_path is None:
        raise ValueError("Chemistry repository requires project_db.db_path.")
    return Path(db_path).expanduser().resolve()


_ROW_FIELDS = (
    "id", "stored_path", "current_path", "current_model_index", "input_format", "extra_data", "has_3d",
)


def scope_spec(
    *,
    role_flag: str,
    molecule_set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = _ROW_FIELDS,
) -> QuerySpec:
    """The scope of a chemistry job, fully declared: it is the tool's only WHERE.

    Counting and iterating are not chemistry-specific — they are `db_count` / `db_pages` over
    this spec. Both coming from here is what keeps `total_chunks` and the feed from disagreeing.
    """
    scope_filters = dict(filters or {})
    limit = scope_filters.pop("_limit", None)
    scope_filters.setdefault("excluded", False)
    resolved_filters: dict[str, Any] = {
        "stored_path__is_not_null": True,
        "stored_path__ne": "",
    }
    if "molecule_type" not in scope_filters:
        resolved_filters[role_flag] = True
    resolved_filters.update(scope_filters)
    if molecule_set_id is not None:
        resolved_filters["id__in_subquery"] = molecule_set_spec(int(molecule_set_id))
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=tuple(fields),
        filters=resolved_filters,
        limit=None if limit is None else int(limit),
    )


def iter_ligand_rows(
    project_db,
    *,
    ligand_set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = _ROW_FIELDS,
    batch_size: int = 128,
) -> Iterator[dict[str, Any]]:
    yield from db_pages(
        project_db,
        scope_spec(role_flag="is_ligand", molecule_set_id=ligand_set_id, filters=filters, fields=fields),
        page_size=max(1, int(batch_size)),
    )


def iter_receptor_rows(
    project_db,
    *,
    receptor_set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = _ROW_FIELDS,
    batch_size: int = 32,
) -> Iterator[dict[str, Any]]:
    yield from db_pages(
        project_db,
        scope_spec(role_flag="is_receptor", molecule_set_id=receptor_set_id, filters=filters, fields=fields),
        page_size=max(1, int(batch_size)),
    )


def max_model_index_by_molecule_ids(project_db, molecule_ids: Sequence[int]) -> dict[int, int]:
    normalized_ids = sorted({int(item) for item in molecule_ids if int(item) > 0})
    if not normalized_ids:
        return {}
    with project_db.get_session() as session:
        rows = session.exec(
            select(MoleculeModel.molecule_id, func.max(MoleculeModel.model_index))
            .where(MoleculeModel.molecule_id.in_(normalized_ids))
            .group_by(MoleculeModel.molecule_id)
        ).all()
    return {int(molecule_id): int(max_index) for molecule_id, max_index in rows if molecule_id is not None and max_index is not None}


def _persist_molecule_updates(db_path: str | Path, *, updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    project_db = ProjectStore()
    project_db.connect(Path(db_path).expanduser().resolve().parent)
    try:
        now = datetime.now()
        with project_db.get_session() as session:
            for update in updates:
                record = session.get(MoleculeRecord, int(update["entity_id"]))
                if record is None:
                    continue
                state = dict(update.get("state") or {})
                record.extra_data = dict(update.get("extra_data") or {})
                if "current_path" in update:
                    record.current_path = str(update.get("current_path") or "")
                if "current_model_index" in update:
                    current_model_index = update.get("current_model_index")
                    record.current_model_index = None if current_model_index is None else int(current_model_index)
                record.has_3d = bool(state.get("has_3d", record.has_3d))
                record.has_hs = bool(state.get("has_hs", record.has_hs))
                record.is_minimized = bool(state.get("is_minimized", record.is_minimized))
                record.conformer_count = max(0, int(state.get("conformer_count", record.conformer_count) or 0))
                record.updated_at = now
                session.add(record)

                for model_row in list(update.get("model_rows") or []):
                    session.add(MoleculeModel(**model_row))
            session.commit()
    finally:
        project_db.disconnect()


def persist_ligand_updates(db_path: str | Path, *, updates: list[dict[str, Any]]) -> None:
    _persist_molecule_updates(db_path, updates=updates)


def persist_receptor_updates(db_path: str | Path, *, updates: list[dict[str, Any]]) -> None:
    _persist_molecule_updates(db_path, updates=updates)


__all__ = [
    "iter_ligand_rows",
    "iter_receptor_rows",
    "max_model_index_by_molecule_ids",
    "persist_ligand_updates",
    "persist_receptor_updates",
    "project_db_path",
    "resolve_ligand_storage_dir",
    "resolve_receptor_storage_dir",
]
