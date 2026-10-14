"""Cascade-aware row deletion for the project DB — pure/off-thread-safe (no Qt).

Deleting a molecule also drops the rows that reference it (docking results, complexes,
descriptors, fingerprints, binding sites, engine states) so the project never keeps
orphaned children. A docking result never goes alone either: `delete_results_where` takes what
was computed from it, files included. Each delete runs in its own session and suppresses "no such table"
(some tables — e.g. docking_results — are created lazily on first use)."""
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from sqlite3 import OperationalError
from typing import Iterable

from sqlalchemy import delete
from sqlmodel import or_, select

from amdockvs.models import (
    BindingSite,
    ComplexRecord,
    DescriptorVectorRecord,
    DockingResultRecord,
    EngineState,
    FingerprintRecord,
    InteractionsResult,
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


def delete_results_where(project_db, condition) -> int:
    """Delete the docking results that meet `condition` and, before them, what was computed from
    them: their interactions and their files. The one way a result row leaves the project. Returns
    how many results."""
    record = DockingResultRecord
    doomed = select(record.id).where(condition)
    poses: set[str] = set()
    held: set[str] = set()  # the poses a molecule has as its structure: a promoted hit's only 3D
    with suppress(OperationalError), project_db.get_session() as session:
        poses = set(session.exec(select(record.pose_path).where(condition).distinct()).all())
        structures = select(MoleculeRecord.stored_path).union(select(MoleculeRecord.current_path))
        held = set(session.exec(select(record.pose_path).where(condition, record.pose_path.in_(structures))).all())
    deleted = 0
    for model, which in ((InteractionsResult, InteractionsResult.docking_result_id.in_(doomed)),
                         (DockingResultRecord, condition)):
        with suppress(OperationalError):  # either table may not exist yet
            with project_db.get_session() as session:
                deleted = session.exec(delete(model).where(which)).rowcount
                session.commit()
    database = getattr(project_db, "db_path", None)  # none in memory: no project folder, no files
    if poses and database is not None:
        with suppress(OperationalError), project_db.get_session() as session:
            held |= set(session.exec(select(record.pose_path).distinct()).all())  # what other results still show
        _remove_result_files(Path(database).parent, poses, held)
    return int(deleted or 0)


def _remove_result_files(project_root: Path, poses: set[str], held: set[str]) -> None:
    """Remove the diagrams drawn from `poses` and, for those not in `held`, the pose and the engine's
    own output next to it. Nothing outside the project is touched.

    ponytail: a pose that keeps some of its rows loses every diagram, also those of the rows that
    stay; they are drawn again on demand."""
    root = project_root.resolve()
    folders: dict[Path, dict[str, bool]] = {}  # folder -> pose file name -> the pose goes too
    for pose in filter(None, poses):
        path = (root / pose).resolve()  # a relative path is the project's; an absolute one stays as it is
        if path.is_relative_to(root):
            folders.setdefault(path.parent, {})[path.name] = pose not in held
    for folder, names in folders.items():
        with suppress(OSError):
            for entry in list(os.scandir(folder)):  # one listing per folder, however many poses
                name = entry.name
                diagram = ".diagram." in name and name.rsplit(".rank", 1)[0] in names  # see diagram_path_for
                twin = name.endswith(".pdbqt") and names.get(f"{name[:-6]}.sdf")  # Vina's output beside the SDF
                if diagram or twin or names.get(name):
                    with suppress(OSError):
                        os.unlink(entry.path)


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
    delete_results_where(
        project_db,
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
        delete_results_where(
            project_db,
            (DockingResultRecord.receptor_molecule_id == receptor_id)
            & (DockingResultRecord.ligand_molecule_id == ligand_id),
        )
    return _safe_delete(project_db, ComplexRecord, ComplexRecord.id.in_(ids))


def delete_docking_results(project_db, result_ids: Iterable[int]) -> int:
    ids = _ids(result_ids)
    if not ids:
        return 0
    return delete_results_where(project_db, DockingResultRecord.id.in_(ids))


__all__ = [
    "delete_binding_sites",
    "delete_molecules",
    "delete_complexes",
    "delete_docking_results",
]
