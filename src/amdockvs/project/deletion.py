"""Cascade-aware row deletion for the project DB — pure/off-thread-safe (no Qt).

Deleting a molecule also drops every row that references it and the files only it used, so the
project never keeps orphaned children. Editing one drops what was computed from its structure
(`clear_derived`): what stays is what describes the compound, not the drawing. A docking result
never goes alone either: `delete_results_where` takes what was computed from it, files included.
Each delete runs in its own session and suppresses "no such table" (some tables — e.g.
docking_results — are created lazily on first use)."""
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from sqlite3 import OperationalError as _SqliteOperationalError
from typing import Iterable

from sqlalchemy import delete
from sqlalchemy.exc import OperationalError as _SAOperationalError
from sqlmodel import or_, select

from amdockvs.core.payloads import PAYLOAD_CACHE
from amdockvs.models import (
    BindingSite,
    ClusteringResult,
    ComplexRecord,
    ConsensusScore,
    DescriptorBlockRecord,
    DescriptorVectorRecord,
    DockingResultRecord,
    EngineState,
    FingerprintRecord,
    InteractionsResult,
    LigandActivity,
    MoleculeModel,
    MoleculeRecord,
    MoleculeRepresentation,
    MoleculeSetMember,
    MoleculeSourceProperty,
    QSARPrediction,
    SimilarityResult,
)

# A session raises SQLAlchemy's error, a raw cursor sqlite3's: a missing table is either.
OperationalError = (_SqliteOperationalError, _SAOperationalError)


def _ids(values: Iterable[int]) -> set[int]:
    return {int(v) for v in values if int(v) > 0}


def _safe_delete(project_db, model, condition) -> int:
    with suppress(*OperationalError):  # table may not exist yet (lazily created)
        with project_db.get_session() as session:
            rows = session.exec(select(model).where(condition)).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)
    return 0


def _safe_update(project_db, model, condition, values: dict) -> int:
    with suppress(*OperationalError):
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
    them: their interactions, their pose payloads and the diagrams cached beside them. The one way a
    result row leaves the project. Returns how many results."""
    record = DockingResultRecord
    doomed = select(record.id).where(condition)
    poses: set[int] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        poses = set(filter(None, session.exec(select(record.pose_payload_id).where(condition)).all()))
    deleted = 0
    for model, which in ((InteractionsResult, InteractionsResult.docking_result_id.in_(doomed)),
                         (DockingResultRecord, condition)):
        with suppress(*OperationalError):  # either table may not exist yet
            with project_db.get_session() as session:
                deleted = session.exec(delete(model).where(which)).rowcount
                session.commit()
    if poses and getattr(project_db, "db_path", None) is not None:  # none in memory: no payloads, no cache
        _release_poses(project_db, poses)
    return int(deleted or 0)


def _release_poses(project_db, poses: set[int]) -> None:
    """Release the pose payloads nothing references any more (a promoted hit keeps its pose as its
    structure), and the files cached under their ids: the pose and its diagrams (see diagram_path_for)."""
    from ms_flow.core.database import resolve_project_store

    store = resolve_project_store(project_db)
    store.payloads.release(poses)
    cached: dict[int, list[str]] = {}  # only the poses someone opened: few, however many went
    with suppress(OSError):
        for entry in os.scandir(Path(store.db_path).parent / PAYLOAD_CACHE):
            head = entry.name.split(".", 1)[0]
            if head.isdigit() and int(head) in poses:
                cached.setdefault(int(head), []).append(entry.path)
    for pid in cached.keys() - store.payloads.kinds(cached).keys():
        for path in cached[pid]:
            with suppress(OSError):
                os.unlink(path)


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


def _owned_files(project_db, ids: set[int], *, structure: bool) -> set[str]:
    """The files the molecules' rows name: models and engine preparations, and with `structure`
    their own stored and current files."""
    paths: set[str] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        for row in session.exec(select(MoleculeModel).where(MoleculeModel.molecule_id.in_(ids))).all():
            paths |= {row.file_path, *(str(v) for v in dict(row.files or {}).values())}
    with suppress(*OperationalError), project_db.get_session() as session:
        for files in session.exec(select(EngineState.files).where(EngineState.molecule_id.in_(ids))).all():
            paths |= {str(v) for v in dict(files or {}).values()}
    if structure:
        with suppress(*OperationalError), project_db.get_session() as session:
            for stored, current in session.exec(
                select(MoleculeRecord.stored_path, MoleculeRecord.current_path).where(MoleculeRecord.id.in_(ids))
            ).all():
                paths |= {stored, current}
    return {str(path) for path in paths if str(path or "").strip()}


def remove_unreferenced_files(project_db, paths: Iterable[str]) -> int:
    """Unlink the project files in `paths` that no remaining molecule or model still names.

    Nothing outside the project goes, nor a shard container (one holds many molecules), nor a
    directory. Returns how many files were removed."""
    database = getattr(project_db, "db_path", None)  # none in memory: no project folder, no files
    paths = {str(path) for path in paths if str(path or "").strip()}
    if database is None or not paths:
        return 0
    held: set[str] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        held |= set(session.exec(select(MoleculeRecord.stored_path).where(MoleculeRecord.stored_path.in_(paths))).all())
        held |= set(session.exec(select(MoleculeRecord.current_path).where(MoleculeRecord.current_path.in_(paths))).all())
        held |= set(session.exec(select(MoleculeModel.file_path).where(MoleculeModel.file_path.in_(paths))).all())
    root = Path(database).parent.resolve()
    removed = 0
    for raw in paths - held:
        path = (root / raw).resolve()  # a relative path is the project's; an absolute one stays as it is
        if path.is_relative_to(root) and path.suffix != ".mshard" and path.is_file():
            with suppress(OSError):
                path.unlink()
                removed += 1
    return removed


def clear_derived(project_db, molecule_ids: Iterable[int]) -> int:
    """Drop what was computed from the molecules' structure, before a new one replaces it: models
    (conformers), engine preparations, descriptors, fingerprints, docking results, consensus,
    similarity hits, QSAR predictions and cluster memberships, files included. What describes the compound —
    activities, source properties, sets, complexes, binding sites — stays.

    The structure files themselves stay: the caller points the row at the new one first, then
    hands the old one to `remove_unreferenced_files`. Returns the docking results deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    files = _owned_files(project_db, ids, structure=False)
    for model in (DescriptorVectorRecord, DescriptorBlockRecord, FingerprintRecord, EngineState,
                  MoleculeModel, QSARPrediction, ClusteringResult, MoleculeRepresentation):
        _safe_delete(project_db, model, model.molecule_id.in_(ids))
    for model, left, right in (
        (ConsensusScore, ConsensusScore.receptor_molecule_id, ConsensusScore.ligand_molecule_id),
        (SimilarityResult, SimilarityResult.query_id, SimilarityResult.target_id),
    ):
        _safe_delete(project_db, model, or_(left.in_(ids), right.in_(ids)))
    results = delete_results_where(
        project_db,
        or_(
            DockingResultRecord.receptor_molecule_id.in_(ids),
            DockingResultRecord.ligand_molecule_id.in_(ids),
        ),
    )
    remove_unreferenced_files(project_db, files)
    return results


def delete_molecules(project_db, molecule_ids: Iterable[int]) -> int:
    """Delete molecules, every row that references them and the files only they used. Returns the
    molecule count deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    structures = _owned_files(project_db, ids, structure=True)
    clear_derived(project_db, ids)
    # Children before parents: project databases enforce foreign keys.
    for model in (LigandActivity, MoleculeSourceProperty, MoleculeSetMember):
        _safe_delete(project_db, model, model.molecule_id.in_(ids))
    _safe_delete(
        project_db,
        ComplexRecord,
        or_(ComplexRecord.receptor_molecule_id.in_(ids), ComplexRecord.ligand_molecule_id.in_(ids)),
    )
    sites: list[int] = []
    with suppress(*OperationalError), project_db.get_session() as session:
        sites = list(session.exec(select(BindingSite.id).where(BindingSite.molecule_id.in_(ids))).all())
    delete_binding_sites(project_db, sites)  # clears the molecules' pointers at them first
    deleted = _safe_delete(project_db, MoleculeRecord, MoleculeRecord.id.in_(ids))
    remove_unreferenced_files(project_db, structures)  # after the rows: a split fragment's sibling may still name one
    return deleted


def delete_complexes(project_db, complex_ids: Iterable[int]) -> int:
    """Delete complex pairs and their docking results (the molecules themselves are kept)."""
    ids = _ids(complex_ids)
    if not ids:
        return 0
    pairs: list[tuple[int, int]] = []
    with suppress(*OperationalError):
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
    "clear_derived",
    "delete_binding_sites",
    "delete_molecules",
    "delete_complexes",
    "delete_docking_results",
    "remove_unreferenced_files",
]
