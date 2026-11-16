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
import shutil
from contextlib import suppress
from pathlib import Path
from sqlite3 import OperationalError as _SqliteOperationalError
from typing import Iterable

from sqlalchemy import delete
from sqlalchemy.exc import OperationalError as _SAOperationalError
from sqlmodel import or_, select

from amdockvs.core.payloads import PAYLOAD_CACHE, payload_store
from amdockvs.core.vocab import MoleculeUsageClass
from amdockvs.models import (
    BindingSite,
    ClusteringResult,
    ComplexRecord,
    DescriptorBlockRecord,
    DescriptorVectorRecord,
    DockingResultRecord,
    FingerprintRecord,
    InteractionsResult,
    LigandPrep,
    LigandActivity,
    MoleculeModel,
    MoleculeRecord,
    MoleculeRepresentation,
    MoleculeSetMember,
    QSARModelMember,
    QSARPrediction,
    ReceptorPrep,
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


def _prediction_dir(site: BindingSite) -> Path | None:
    """The directory holding one prediction of one receptor in one run, as the site's own stored
    paths name it: ``<pocket predictions>/<run_id>/receptor_<molecule_id>``.

    Read from a path up, and only accepted when the directory is named after this very site's
    receptor and sits inside one named after its own run. A path copied from another run, or
    anything outside that layout, therefore names nothing here.
    """
    if str(site.source or "") != "p2rank":
        return None
    extra = dict(site.extra_data or {})
    run = str(extra.get("run_id") or "").strip()
    if not run:
        return None
    receptor = f"receptor_{int(site.molecule_id)}"
    for key in ("predictions_path", "points_path", "pymol_script_path"):
        value = str(extra.get(key) or "").strip()
        if not value:
            continue
        for parent in Path(value).parents:
            if parent.name == receptor and parent.parent.name == run:
                return parent
    return None


def _referenced_paths(site: BindingSite) -> set[Path]:
    extra = dict(site.extra_data or {})
    return {
        Path(value)
        for value in (
            str(extra.get(key) or "").strip()
            for key in ("predictions_path", "points_path", "pymol_script_path")
        )
        if value
    }


def _project_root(project_db) -> Path | None:
    root = getattr(project_db, "project_dir", None)
    database = getattr(project_db, "db_path", None)
    if root is None and database is not None:
        root = Path(database).parent
    return None if root is None else Path(root).expanduser().resolve()


def _here(root: Path, path: Path) -> Path | None:
    """That path as *this* project holds it, or None when this project holds nothing by it.

    A site records in full where its outputs were written, so after the project folder is copied
    or moved its rows still name the files of the folder it came from. The copy has the same
    tree under its own root, so the longest tail of the stored path that exists here is this
    project's own copy of it — and the folder it was copied from, which is not under this root,
    is never reached. Deletion only ever touches what comes back from here.
    """
    path = Path(path)
    if root == path or root in path.parents:
        return path
    parts = path.parts
    for index in range(1, len(parts) - 1):  # longest tail first, never a bare last component
        candidate = root.joinpath(*parts[index:])
        if candidate.exists():
            return candidate
    return None


def _drop_unreferenced_predictions(project_db, candidates: set[Path]) -> None:
    """Remove the prediction directories no surviving site points into any more.

    P2Rank writes seven files per receptor and only three of them are named by a site row
    (the predictions table, the points and the PyMOL script); the rest — its log, its
    parameters, the residue table, its copy of the receptor — are referenced by nothing. The
    directory is what one prediction is, so the directory is what goes when the last site
    parsed from it does, the way a deleted result releases its pose.

    Every surviving site is asked what it still points at, whatever its source: only a P2Rank
    site in the stored layout makes a directory a *candidate*, but a manual site carrying those
    same paths reads those same files, and deleting them under it would leave it pointing at
    nothing.
    """
    if not candidates:
        return
    root = _project_root(project_db)
    if root is None:
        return
    surviving: set[Path] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        for site in session.exec(select(BindingSite)).all():
            directory = _prediction_dir(site)
            kept = {path.parent for path in _referenced_paths(site)}
            if directory is not None:
                kept.add(directory)
            for path in kept:
                surviving.add(path)
                mine = _here(root, path)  # what it names here, if this project was copied
                if mine is not None:
                    surviving.add(mine)
    for stored in candidates:
        directory = _here(root, stored)
        if directory is None or root not in directory.parents:  # never outside this project
            continue
        # A path of a surviving site inside it (`extra_data` can be copied between rows) keeps
        # the whole directory: nothing here deletes a file someone still names.
        if any(directory == kept or directory in kept.parents for kept in surviving):
            continue
        with suppress(OSError):
            shutil.rmtree(directory)
        with suppress(OSError):  # the run's own folder, once it holds no receptor
            directory.parent.rmdir()


def delete_binding_sites(project_db, binding_site_ids: Iterable[int]) -> int:
    """Delete binding sites and clear every pointer at them. Returns the count deleted.

    Pocket prediction only ever adds sites, so pruning old runs is the user's call — which means
    this has to leave no dangling reference behind: a receptor whose active site is deleted ends
    up with no active site, and a complex loses its site instead of pointing at a missing row.

    What a pruned prediction leaves on disk goes with it: once no site parsed from a run's
    outputs for a receptor is left, that prediction's directory is removed. Deleting a receptor
    comes through here too, so its predictions do not outlive it.
    """
    ids = _ids(binding_site_ids)
    if not ids:
        return 0
    candidates: set[Path] = set()
    with suppress(*OperationalError), project_db.get_session() as session:
        for site in session.exec(select(BindingSite).where(BindingSite.id.in_(ids))).all():
            directory = _prediction_dir(site)
            if directory is not None:
                candidates.add(directory)
    _safe_update(project_db, ComplexRecord, ComplexRecord.binding_site_id.in_(ids),
                 {"binding_site_id": None})
    deleted = _safe_delete(project_db, BindingSite, BindingSite.id.in_(ids))
    _drop_unreferenced_predictions(project_db, candidates)
    return deleted


def _owned_payloads(project_db, ids: set[int], *, structure: bool) -> set[int]:
    """The payloads the molecules' rows hold: models (and their PAE) and engine preparations, and
    with `structure` their own stored and current structures and what their complexes froze."""
    held: set[int] = set()

    def collect(*columns, where) -> None:
        with suppress(*OperationalError), project_db.get_session() as session:
            for row in session.exec(select(*columns).where(where)).all():
                held.update(row if len(columns) > 1 else (row,))  # one column selects scalars

    collect(MoleculeModel.payload_id, MoleculeModel.data_payload_id, where=MoleculeModel.molecule_id.in_(ids))
    collect(LigandPrep.payload_id, where=LigandPrep.molecule_id.in_(ids))
    collect(ReceptorPrep.rigid_payload_id, ReceptorPrep.flex_payload_id, where=ReceptorPrep.receptor_molecule_id.in_(ids))
    if structure:
        collect(MoleculeRecord.stored_payload_id, MoleculeRecord.current_payload_id,
                MoleculeRecord.properties_payload_id, where=MoleculeRecord.id.in_(ids))
        collect(ComplexRecord.reference_receptor_payload_id, ComplexRecord.reference_ligand_payload_id,
                where=or_(ComplexRecord.receptor_molecule_id.in_(ids), ComplexRecord.ligand_molecule_id.in_(ids)))
    return _ids(filter(None, held))


def clear_derived(project_db, molecule_ids: Iterable[int]) -> int:
    """Drop what was computed from the molecules' structure, before a new one replaces it: models
    (conformers), engine preparations, descriptors, fingerprints, docking results,
    similarity hits, QSAR predictions and cluster memberships, payloads included. What describes the
    compound — activities, sets, complexes, binding sites — stays.

    The structure payloads themselves stay: the caller points the row at the new one first, then
    releases the old one. Returns the docking results deleted."""
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    owned = _owned_payloads(project_db, ids, structure=False)
    _safe_delete(project_db, ReceptorPrep, ReceptorPrep.receptor_molecule_id.in_(ids))
    for model in (DescriptorVectorRecord, DescriptorBlockRecord, FingerprintRecord, LigandPrep,
                  MoleculeModel, QSARPrediction, ClusteringResult, MoleculeRepresentation):
        _safe_delete(project_db, model, model.molecule_id.in_(ids))
    _safe_delete(project_db, SimilarityResult,
                 or_(SimilarityResult.query_id.in_(ids), SimilarityResult.target_id.in_(ids)))
    results = delete_results_where(
        project_db,
        or_(
            DockingResultRecord.receptor_molecule_id.in_(ids),
            DockingResultRecord.ligand_molecule_id.in_(ids),
        ),
    )
    release(project_db, owned)
    return results


def _unshared_reference_ligands(project_db, receptor_ids: set[int]) -> list[int]:
    """Cocrystal references owned only by the receptors being deleted.

    Keep references shared with another receptor or promoted to the general ligand pool.
    """
    with suppress(*OperationalError), project_db.get_session() as session:
        candidates = set(session.exec(
            select(ComplexRecord.ligand_molecule_id)
            .join(MoleculeRecord, MoleculeRecord.id == ComplexRecord.ligand_molecule_id)
            .where(
                ComplexRecord.receptor_molecule_id.in_(receptor_ids),
                ComplexRecord.purpose == "reference",
                MoleculeRecord.usage_class == MoleculeUsageClass.REFERENCE,
                MoleculeRecord.is_receptor.is_(False),
            )
        ).all())
        if candidates:
            shared = set(session.exec(
                select(ComplexRecord.ligand_molecule_id).where(
                    ComplexRecord.ligand_molecule_id.in_(candidates),
                    ComplexRecord.receptor_molecule_id.not_in(receptor_ids),
                )
            ).all())
            return sorted(candidates - shared - set(receptor_ids))
    return []


def delete_molecules(project_db, molecule_ids: Iterable[int]) -> int:
    """Delete molecules and their dependent rows, unshared references and payloads.

    Returns the number of requested molecules deleted, excluding dependent references.
    """
    ids = _ids(molecule_ids)
    if not ids:
        return 0
    references = _unshared_reference_ligands(project_db, ids)
    if references:
        delete_molecules(project_db, references)
    structures = _owned_payloads(project_db, ids, structure=True)
    clear_derived(project_db, ids)
    # Children before parents: project databases enforce foreign keys.
    for model in (LigandActivity, MoleculeSetMember, QSARModelMember):
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
