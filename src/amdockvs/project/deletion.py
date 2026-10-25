"""Cascade-aware row deletion for the project DB — pure/off-thread-safe (no Qt).

Deleting a molecule also drops every row that references it and the payloads only it held, so the
project never keeps orphaned children. Editing one drops what was computed from its structure
(`clear_derived`): what stays is what describes the compound, not the drawing. A docking result
never goes alone either: `delete_results_where` takes what was computed from it, poses included.
Bytes leave with `release` once the rows are gone: it only deletes the payloads no row references
any more, so one that is still shared (a hit's pose, an unedited import) stays. A library shard is never
deleted: one holds many molecules.
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

from amdockvs.core.payloads import PAYLOAD_CACHE, payload_store
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
    poses: set[int] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        poses = set(filter(None, session.exec(select(record.pose_payload_id).where(condition)).all()))
    deleted = 0
    for model, which in ((InteractionsResult, InteractionsResult.pose_payload_id.in_(poses)),
                         (DockingResultRecord, condition)):
        with suppress(*OperationalError):  # either table may not exist yet
            with project_db.get_session() as session:
                deleted = session.exec(delete(model).where(which)).rowcount
                session.commit()
    release(project_db, poses)
    return int(deleted or 0)


def release(project_db, payload_ids: Iterable[int]) -> None:
    """Release the payloads that no row references any more, and the files cached under their ids
    (a structure someone opened, a pose's diagrams: see diagram_path_for). A payload a row still
    holds stays, and so does its cache."""
    ids = _ids(payload_ids)
    if not ids or getattr(project_db, "db_path", None) is None:  # none in memory: no payloads, no cache
        return
    store = payload_store(project_db)
    store.release(ids)
    cached: dict[int, list[str]] = {}  # only what someone opened: few, however many went
    with suppress(OSError):
        for entry in os.scandir(Path(project_db.db_path).parent / PAYLOAD_CACHE):
            head = entry.name.split(".", 1)[0]
            if head.isdigit() and int(head) in ids:
                cached.setdefault(int(head), []).append(entry.path)
    for pid in cached.keys() - store.kinds(cached).keys():
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
    _safe_update(project_db, ComplexRecord, ComplexRecord.binding_site_id.in_(ids),
                 {"binding_site_id": None})
    return _safe_delete(project_db, BindingSite, BindingSite.id.in_(ids))


def _owned_payloads(project_db, ids: set[int], *, structure: bool) -> set[int]:
    """The payloads the molecules' rows hold: models (and their PAE) and engine preparations, and
    with `structure` their own stored and current structures and what their complexes froze."""
    held: set[int] = set()

    def collect(*columns, where) -> None:
        with suppress(*OperationalError), project_db.get_session() as session:
            for row in session.exec(select(*columns).where(where)).all():
                held.update(row)

    collect(MoleculeModel.payload_id, MoleculeModel.data_payload_id, where=MoleculeModel.molecule_id.in_(ids))
    collect(EngineState.prepared_payload_id, EngineState.flex_payload_id, where=EngineState.molecule_id.in_(ids))
    if structure:
        collect(MoleculeRecord.stored_payload_id, MoleculeRecord.current_payload_id,
                MoleculeRecord.properties_payload_id, where=MoleculeRecord.id.in_(ids))
        collect(ComplexRecord.reference_receptor_payload_id, ComplexRecord.reference_ligand_payload_id,
                where=or_(ComplexRecord.receptor_molecule_id.in_(ids), ComplexRecord.ligand_molecule_id.in_(ids)))
    return _ids(filter(None, held))


def clear_derived(project_db, molecule_ids: Iterable[int]) -> int:
    """Drop what was computed from the molecules' structure, before a new one replaces it: models
    (conformers), engine preparations, descriptors, fingerprints, docking results, consensus,
    similarity hits, QSAR predictions and cluster memberships, payloads included. What describes the
    compound — activities, source properties, sets, complexes, binding sites — stays.

    The structure payloads themselves stay: the caller points the row at the new one first, then
    releases the old one. Returns the docking results deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    owned = _owned_payloads(project_db, ids, structure=False)
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
    release(project_db, owned)
    return results


def delete_molecules(project_db, molecule_ids: Iterable[int]) -> int:
    """Delete molecules, every row that references them and the payloads only they held. Returns the
    molecule count deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    structures = _owned_payloads(project_db, ids, structure=True)
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
    release(project_db, structures)  # after the rows: a split fragment's sibling may still hold one
    return deleted


def delete_complexes(project_db, complex_ids: Iterable[int]) -> int:
    """Delete complex pairs and their docking results (the molecules themselves are kept)."""
    ids = _ids(complex_ids)
    if not ids:
        return 0
    pairs: list[tuple[int, int]] = []
    frozen: set[int] = set()
    with suppress(*OperationalError):
        with project_db.get_session() as session:
            for row in session.exec(select(ComplexRecord).where(ComplexRecord.id.in_(ids))).all():
                pairs.append((int(row.receptor_molecule_id), int(row.ligand_molecule_id)))
                frozen |= {row.reference_receptor_payload_id, row.reference_ligand_payload_id}
    for receptor_id, ligand_id in pairs:
        delete_results_where(
            project_db,
            (DockingResultRecord.receptor_molecule_id == receptor_id)
            & (DockingResultRecord.ligand_molecule_id == ligand_id),
        )
    deleted = _safe_delete(project_db, ComplexRecord, ComplexRecord.id.in_(ids))
    release(project_db, filter(None, frozen))
    return deleted


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
    "release",
]
