from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import update as sql_update
from sqlmodel import delete, select

from ms_flow.query import QuerySpec

from amdockvs.constants import TABLE_ENGINES, TABLE_MOLECULE_SET_MEMBERS
from amdockvs.models import MoleculeRecord, SetItemRecord, SetRecord


@dataclass(frozen=True)
class SetRef:
    id: int
    entity_kind: str
    kind: str = "snapshot"
    job_id: str | None = None


@dataclass(frozen=True)
class ComplexSetRef:
    id: int
    kind: str = "snapshot"


@dataclass(frozen=True)
class ComplexRef:
    id: int


@dataclass(frozen=True)
class MoleculeSetRef:
    id: int
    kind: str = "snapshot"
    job_id: str | None = None


@dataclass(frozen=True)
class QSARDatasetRef:
    id: int


@dataclass(frozen=True)
class QSARModelRef:
    id: int


def _normalize_ids(values: Iterable[int | str]) -> list[int]:
    seen: set[int] = set()
    resolved: list[int] = []
    for raw in values:
        value = int(raw)
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        resolved.append(value)
    return resolved


def _sync_molecule_in_set_flags(session, molecule_ids: Iterable[int | str]) -> None:
    affected_ids = _normalize_ids(molecule_ids)
    if not affected_ids:
        return
    remaining_rows = session.exec(
        select(SetItemRecord.molecule_id)
        .where(SetItemRecord.molecule_id.in_(affected_ids))
        .distinct()
    ).all()
    remaining_ids = {int(value) for value in remaining_rows}
    session.exec(
        sql_update(MoleculeRecord)
        .where(MoleculeRecord.id.in_(affected_ids))
        .values(in_set=False)
    )
    if remaining_ids:
        session.exec(
            sql_update(MoleculeRecord)
            .where(MoleculeRecord.id.in_(sorted(remaining_ids)))
            .values(in_set=True)
        )


def sync_all_molecule_in_set_flags(project_db) -> None:
    with project_db.get_session() as session:
        member_rows = session.exec(select(SetItemRecord.molecule_id).distinct()).all()
        member_ids = sorted({int(value) for value in member_rows if int(value) > 0})
        current_rows = session.exec(
            select(MoleculeRecord.id).where(MoleculeRecord.in_set.is_(True))
        ).all()
        current_ids = {int(value) for value in current_rows if int(value) > 0}
        target_ids = set(member_ids)
        ids_to_false = sorted(current_ids - target_ids)
        ids_to_true = sorted(target_ids - current_ids)
        if ids_to_false:
            session.exec(
                sql_update(MoleculeRecord)
                .where(MoleculeRecord.id.in_(ids_to_false))
                .values(in_set=False)
            )
        if ids_to_true:
            session.exec(
                sql_update(MoleculeRecord)
                .where(MoleculeRecord.id.in_(ids_to_true))
                .values(in_set=True)
            )
        session.commit()


def create_set(
    project_db,
    *,
    entity_kind: str,
    name: str,
    kind: str = "",
    is_snapshot: bool = True,
    filter_spec: dict | None = None,
    metadata: dict | None = None,
) -> SetRef:
    if str(entity_kind or "").strip().lower() != "molecule":
        raise NotImplementedError("Only molecule sets are supported by the current schema.")
    with project_db.get_session() as session:
        record = SetRecord(
            name=str(name or "").strip() or "unnamed_set",
            purpose=str(kind or "").strip() or "custom",
            description="",
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return SetRef(id=int(record.id or 0), entity_kind="molecule", kind="snapshot" if is_snapshot else "dynamic")


def replace_set_items(project_db, set_ref: SetRef | int, *, entity_kind: str, entity_ids: Iterable[int | str]) -> SetRef:
    if str(entity_kind or "").strip().lower() != "molecule":
        raise NotImplementedError("Only molecule sets are supported by the current schema.")
    set_id = int(set_ref.id if isinstance(set_ref, SetRef) else set_ref)
    item_ids = _normalize_ids(entity_ids)
    with project_db.get_session() as session:
        previous_ids = session.exec(
            select(SetItemRecord.molecule_id)
            .where(SetItemRecord.set_id == set_id)
        ).all()
        session.exec(delete(SetItemRecord).where(SetItemRecord.set_id == set_id))
        for entity_id in item_ids:
            session.add(SetItemRecord(set_id=set_id, molecule_id=int(entity_id)))
        _sync_molecule_in_set_flags(session, list(previous_ids) + item_ids)
        session.commit()
    return SetRef(id=set_id, entity_kind="molecule", kind="snapshot")


def create_snapshot_set(
    project_db,
    *,
    entity_kind: str,
    name: str,
    entity_ids: Iterable[int | str],
    kind: str = "",
    metadata: dict | None = None,
) -> SetRef:
    ref = create_set(project_db, entity_kind=entity_kind, name=name, kind=kind, is_snapshot=True, metadata=metadata)
    return replace_set_items(project_db, ref, entity_kind=entity_kind, entity_ids=entity_ids)


def list_set_ids(project_db, set_ref: SetRef | MoleculeSetRef | ComplexSetRef | int) -> list[int]:
    set_id = int(set_ref.id if hasattr(set_ref, "id") else set_ref)
    with project_db.get_session() as session:
        rows = session.exec(
            select(SetItemRecord.molecule_id)
            .where(SetItemRecord.set_id == set_id)
            .order_by(SetItemRecord.molecule_id)
        ).all()
    return [int(value) for value in rows]


def get_set(project_db, set_ref: SetRef | MoleculeSetRef | ComplexSetRef | int) -> SetRecord | None:
    set_id = int(set_ref.id if hasattr(set_ref, "id") else set_ref)
    with project_db.get_session() as session:
        return session.get(SetRecord, set_id)


def _as_complex(ref: SetRef) -> ComplexSetRef:
    return ComplexSetRef(id=ref.id, kind=ref.kind)


def _as_molecule(ref: SetRef) -> MoleculeSetRef:
    return MoleculeSetRef(id=ref.id, kind=ref.kind, job_id=ref.job_id)


def create_complex_set(project_db, **kwargs) -> ComplexSetRef:
    return _as_complex(create_set(project_db, entity_kind="complex", **kwargs))


def create_molecule_set(project_db, **kwargs) -> MoleculeSetRef:
    return _as_molecule(create_set(project_db, entity_kind="molecule", **kwargs))


def create_complex_snapshot_set(project_db, *, complex_ids: Iterable[int | str], **kwargs) -> ComplexSetRef:
    return _as_complex(create_snapshot_set(project_db, entity_kind="complex", entity_ids=complex_ids, **kwargs))


def create_molecule_snapshot_set(project_db, *, molecule_ids: Iterable[int | str], **kwargs) -> MoleculeSetRef:
    return _as_molecule(create_snapshot_set(project_db, entity_kind="molecule", entity_ids=molecule_ids, **kwargs))


def replace_complex_set_items(project_db, set_ref: ComplexSetRef | int, complex_ids: Iterable[int | str]) -> ComplexSetRef:
    return _as_complex(replace_set_items(project_db, int(set_ref.id if isinstance(set_ref, ComplexSetRef) else set_ref), entity_kind="complex", entity_ids=complex_ids))


def replace_molecule_set_items(project_db, set_ref: MoleculeSetRef | int, molecule_ids: Iterable[int | str]) -> MoleculeSetRef:
    return _as_molecule(replace_set_items(project_db, int(set_ref.id if isinstance(set_ref, MoleculeSetRef) else set_ref), entity_kind="molecule", entity_ids=molecule_ids))


def list_complex_set_ids(project_db, set_ref: ComplexSetRef | int) -> list[int]:
    return list_set_ids(project_db, set_ref)


def list_molecule_set_ids(project_db, set_ref: MoleculeSetRef | int) -> list[int]:
    return list_set_ids(project_db, set_ref)


# --- scope subqueries --------------------------------------------------------
# Membership and preparation state live in other tables. They are declared as single-column
# QuerySpec and injected as `id__in_subquery` / `id__not_in_subquery`, so the database resolves
# them. Nothing of library size is ever built in Python (and `IN (?,?,...)` caps at 250k anyway).

def molecule_set_spec(set_id: int) -> QuerySpec:
    return QuerySpec(
        table=TABLE_MOLECULE_SET_MEMBERS,
        fields=("molecule_id",),
        filters={"set_id": int(set_id)},
    )


def prepared_molecules_spec(*, role_type: str, engine: str, is_ready: bool = True) -> QuerySpec:
    return QuerySpec(
        table=TABLE_ENGINES,
        fields=("molecule_id",),
        filters={
            "role_type": str(role_type).strip().lower(),
            "engine": str(engine).strip().lower(),
            "is_ready": bool(is_ready),
        },
    )


def get_complex_set(project_db, set_ref: ComplexSetRef | int) -> SetRecord | None:
    return get_set(project_db, set_ref)


def get_molecule_set(project_db, set_ref: MoleculeSetRef | int) -> SetRecord | None:
    return get_set(project_db, set_ref)


__all__ = [
    "ComplexRef",
    "ComplexSetRef",
    "MoleculeSetRef",
    "QSARDatasetRef",
    "QSARModelRef",
    "SetRef",
    "create_complex_set",
    "create_complex_snapshot_set",
    "create_molecule_set",
    "create_molecule_snapshot_set",
    "get_complex_set",
    "get_molecule_set",
    "list_complex_set_ids",
    "list_molecule_set_ids",
    "molecule_set_spec",
    "prepared_molecules_spec",
    "replace_complex_set_items",
    "replace_molecule_set_items",
    "sync_all_molecule_in_set_flags",
]
