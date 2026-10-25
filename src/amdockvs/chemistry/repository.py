from __future__ import annotations

from typing import Any, Iterator, Mapping, Sequence

from sqlalchemy import func, select

from ms_flow.query import QuerySpec, db_pages

from amdockvs.core.constants import TABLE_MOLECULES
from amdockvs.core.vocab import ReprType
from amdockvs.models import MoleculeModel, MoleculeRepresentation
from amdockvs.molecules.storage import as_store
from amdockvs.project.sets import molecule_set_spec


_ROW_FIELDS = (
    "id", "stored_payload_id", "current_payload_id", "extra_data", "has_3d",
)


def scope_spec(
    *,
    role_flag: str,
    molecule_set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = _ROW_FIELDS,
    require_structure: bool = True,
) -> QuerySpec:
    """The scope of a chemistry job, fully declared: it is the tool's only WHERE.

    Counting and iterating are not chemistry-specific — they are `db_count` / `db_pages` over
    this spec. Both coming from here is what keeps `total_chunks` and the feed from disagreeing.
    `require_structure=False` is structure prediction: sequence-only proteins have no structure yet.
    """
    scope_filters = dict(filters or {})
    limit = scope_filters.pop("_limit", None)
    scope_filters.setdefault("excluded", False)
    resolved_filters: dict[str, Any] = (
        {"stored_payload_id__is_not_null": True} if require_structure else {}
    )
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
    source,
    *,
    ligand_set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = _ROW_FIELDS,
    batch_size: int = 128,
) -> Iterator[dict[str, Any]]:
    """`source` is the ligand store, or the project db (which is what `vs` means)."""
    yield from as_store(source).iter_rows(
        scope_spec(role_flag="is_ligand", molecule_set_id=ligand_set_id, filters=filters, fields=fields),
        batch_size=max(1, int(batch_size)),
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


def sequences_by_molecule_ids(project_db, molecule_ids: Sequence[int]) -> dict[int, str]:
    normalized_ids = sorted({int(item) for item in molecule_ids if int(item) > 0})
    if not normalized_ids:
        return {}
    with project_db.get_session() as session:
        rows = session.exec(
            select(MoleculeRepresentation.molecule_id, MoleculeRepresentation.value)
            .where(MoleculeRepresentation.repr_type == ReprType.SEQUENCE_AA)
            .where(MoleculeRepresentation.molecule_id.in_(normalized_ids))
        ).all()
    return {int(molecule_id): str(value or "") for molecule_id, value in rows}


__all__ = [
    "iter_ligand_rows",
    "iter_receptor_rows",
    "max_model_index_by_molecule_ids",
]
