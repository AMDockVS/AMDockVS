"""Cascade-aware row deletion for the project DB — pure/off-thread-safe (no Qt).

Deleting a molecule also drops the rows that reference it (docking results, complexes,
descriptors, fingerprints, binding sites, engine states) so the project never keeps
orphaned children. Each delete runs in its own session and suppresses "no such table"
(some tables — e.g. docking_results — are created lazily on first use)."""
from __future__ import annotations

from contextlib import suppress
from sqlite3 import OperationalError
from typing import Iterable

from sqlmodel import or_, select

from amdockvs.models import (
    BindingSite,
    ComplexRecord,
    DescriptorVectorRecord,
    DockingResultRecord,
    EngineState,
    FingerprintRecord,
    MoleculeRecord,
)


def _ids(values: Iterable[int]) -> set[int]:
    return {int(v) for v in values if int(v) > 0}


def _safe_delete(project_db, model, condition) -> int:
    with suppress(OperationalError):  # table may not exist yet (lazily created)
        with project_db.get_session() as session:
            rows = session.exec(select(model).where(condition)).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)
    return 0


def _safe_update(project_db, model, condition, values: dict) -> int:
    with suppress(OperationalError):
        with project_db.get_session() as session:
            rows = session.exec(select(model).where(condition)).all()
            for row in rows:
                for field, value in values.items():
                    setattr(row, field, value)
                session.add(row)
            session.commit()
            return len(rows)
    return 0


def delete_binding_sites(project_db, binding_site_ids: Iterable[int]) -> int:
    """Delete binding sites and clear every pointer at them. Returns the count deleted.

    Pocket prediction only ever adds sites, so pruning old runs is the user's call — which means
    this has to leave no dangling reference behind: a receptor whose active site is deleted ends
    up with no active site, and a complex loses its site instead of pointing at a missing row.
    """
    ids = _ids(binding_site_ids)
    if not ids:
        return 0
    _safe_update(project_db, MoleculeRecord, MoleculeRecord.active_binding_site_id.in_(ids),
                 {"active_binding_site_id": None})
    _safe_update(project_db, ComplexRecord, ComplexRecord.binding_site_id.in_(ids),
                 {"binding_site_id": None})
    return _safe_delete(project_db, BindingSite, BindingSite.id.in_(ids))


def delete_molecules(project_db, molecule_ids: Iterable[int]) -> int:
    """Delete molecules and every row that references them. Returns the molecule count deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    # Children first (SQLite FK enforcement is off by default, but order keeps it correct if on).
    _safe_delete(project_db, DescriptorVectorRecord, DescriptorVectorRecord.molecule_id.in_(ids))
    _safe_delete(project_db, FingerprintRecord, FingerprintRecord.molecule_id.in_(ids))
    _safe_delete(project_db, BindingSite, BindingSite.molecule_id.in_(ids))
    _safe_delete(project_db, EngineState, EngineState.molecule_id.in_(ids))
    _safe_delete(
        project_db,
        DockingResultRecord,
        or_(
            DockingResultRecord.receptor_molecule_id.in_(ids),
            DockingResultRecord.ligand_molecule_id.in_(ids),
        ),
    )
    _safe_delete(
        project_db,
        ComplexRecord,
        or_(ComplexRecord.receptor_molecule_id.in_(ids), ComplexRecord.ligand_molecule_id.in_(ids)),
    )
    return _safe_delete(project_db, MoleculeRecord, MoleculeRecord.id.in_(ids))


def delete_complexes(project_db, complex_ids: Iterable[int]) -> int:
    """Delete complex pairs and their docking results (the molecules themselves are kept)."""
    ids = _ids(complex_ids)
    if not ids:
        return 0
    pairs: list[tuple[int, int]] = []
    with suppress(OperationalError):
        with project_db.get_session() as session:
            for row in session.exec(select(ComplexRecord).where(ComplexRecord.id.in_(ids))).all():
                pairs.append((int(row.receptor_molecule_id), int(row.ligand_molecule_id)))
    for receptor_id, ligand_id in pairs:
        _safe_delete(
            project_db,
            DockingResultRecord,
            (DockingResultRecord.receptor_molecule_id == receptor_id)
            & (DockingResultRecord.ligand_molecule_id == ligand_id),
        )
    return _safe_delete(project_db, ComplexRecord, ComplexRecord.id.in_(ids))


def delete_docking_results(project_db, result_ids: Iterable[int]) -> int:
    ids = _ids(result_ids)
    if not ids:
        return 0
    return _safe_delete(project_db, DockingResultRecord, DockingResultRecord.id.in_(ids))


__all__ = [
    "delete_binding_sites",
    "delete_molecules",
    "delete_complexes",
    "delete_docking_results",
]
