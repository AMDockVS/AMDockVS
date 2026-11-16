"""One current screening configuration per program; redocking keeps its comparisons."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import and_, delete, exists, or_
from sqlmodel import select

from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.results.dataset import fragment_ligand_ids, selection_covers
from amdockvs.docking.results.repository import _decode_metadata, supersede_results
from amdockvs.models import (
    DockingResultRecord, DockingRun, MoleculeRecord, ScreeningShard, ScreeningShardRun, ScreeningTarget,
)
from amdockvs.core.vocab import TargetState
from amdockvs.project.deletion import delete_results_where


class DockingProtocolConflictError(RuntimeError):
    code = "docking_protocol_conflict"

    def __init__(self, *, requested: dict, previous: list[dict]) -> None:
        self.requested_protocol = requested
        self.previous_protocols = previous
        labels = "; ".join(sorted({str(row["protocol_label"]) for row in previous}))
        super().__init__(
            f"Docking results already exist with another configuration: {labels}. "
            f"Requested: {requested.get('label') or requested['program']}. "
            "Run again with replace_existing=True to delete this program's previous docking "
            "results throughout the project and start over. Redocking results are preserved."
        )


def resolved_protocol(program, config: dict, metadata: dict | None) -> dict:
    payload = dict(metadata or {})
    payload.update(program=program.key, config=dict(config), hash="")
    payload.setdefault("label", program.label)
    return DockingProtocolMetadata.from_mapping(payload).as_metrics_payload()


def _program_runs(project_db, program, *, with_results: bool = True) -> list[DockingRun]:
    with project_db.get_session() as session:
        query = select(DockingRun).where(
            DockingRun.engine == program.docking_engine, DockingRun.run_kind == "screening",
        )
        if with_results:
            query = query.where(or_(
                exists().where(DockingResultRecord.docking_run_id == DockingRun.id),
                exists().where(ScreeningShardRun.docking_run_id == DockingRun.id),
            ))
        runs = session.exec(query).all()
    return [run for run in runs if (_decode_metadata(run.protocol).get("program") or program.key) == program.key]


def stored_protocol_hash(project_db, *, program, protocol_hash: str, runs: list[DockingRun] | None = None) -> str:
    """The hash a run of this configuration docks and skips under: `protocol_hash`, or the hash
    its results are stored under when an older project hashed the same configuration with fewer
    fields, so an unchanged saved configuration still resumes. Never raises a conflict: the run
    preview asks it too, and must count what the run will skip."""
    runs = _program_runs(project_db, program) if runs is None else runs
    if runs and len({run.protocol_hash for run in runs}) == 1:
        saved = _decode_metadata(runs[0].protocol)
        if saved.get("config") and resolved_protocol(
            program, program.validate_config(saved["config"]), saved,
        )["hash"] == protocol_hash:
            return str(runs[0].protocol_hash)
    return str(protocol_hash)


def check_protocol_conflict(project_db, *, program, protocol: dict, replace_existing: bool) -> bool:
    runs = _program_runs(project_db, program)
    protocol["hash"] = stored_protocol_hash(project_db, program=program, protocol_hash=protocol["hash"], runs=runs)
    previous = [run.model_dump(mode="python") for run in runs if run.protocol_hash != protocol["hash"]]
    if previous and not replace_existing:
        raise DockingProtocolConflictError(requested=protocol, previous=previous)
    return bool(previous)


def clear_previous_docking(project_db, *, program) -> None:
    """Delete screening results only after the caller explicitly confirms replacement."""
    run_ids = [run.id for run in _program_runs(project_db, program, with_results=False)]
    if not run_ids:
        return
    with project_db.get_session() as session:
        paths = list(session.exec(select(ScreeningShardRun.result_path).where(
            ScreeningShardRun.docking_run_id.in_(run_ids), ScreeningShardRun.result_path != "",
        )).all())
    delete_results_where(project_db, DockingResultRecord.docking_run_id.in_(run_ids))
    with project_db.get_session() as session:
        session.exec(delete(ScreeningShardRun).where(ScreeningShardRun.docking_run_id.in_(run_ids)))
        session.exec(delete(ScreeningTarget).where(ScreeningTarget.docking_run_id.in_(run_ids)))
        session.commit()
    root = getattr(project_db, "project_dir", None) or Path(project_db.db_path).parent
    for value in set(paths):
        path = Path(value)
        (path if path.is_absolute() else Path(root) / path).unlink(missing_ok=True)


def supersede_shard_runs(project_db, *, docking_run_id: int | None) -> int:
    """A forced campaign completed: what it docked again replaces what older runs of the same
    engine and protocol had for those shard/receptor pairs, instead of sitting beside it.

    Only a receipt this run's own selection covers is replaced: an off-target stage docks the
    Top-N picked from one reference receptor, not the shard, so its receipt leaves an older
    whole-shard receipt — and the fragment and results behind it — alone. The pairs it did
    recalculate still lose their older rows, through `supersede_results`.

    The receipts it does replace go with their result fragments, and so do the materialized
    results those fragments were the evidence for: each retired fragment names the shard records
    it holds, which identify the ligands (a library molecule is its batch and its shard record).
    A hit that no longer qualifies under the new ranking would otherwise stay behind with
    nothing supporting it. A receptor an older run is left with no receipt at all loses whatever
    results remain there and its plan row with them. A result of a pair the new run also
    materialized goes in any case. Returns how many receipts were superseded."""
    if not docking_run_id:
        return 0
    root = getattr(project_db, "project_dir", None) or Path(project_db.db_path).parent
    with project_db.get_session() as session:
        current = session.get(DockingRun, int(docking_run_id))
        if current is None:
            return 0
        # One receipt per (shard, receptor) in a run, so the selection it was docked under is
        # single-valued here: `""` for the whole shard, `top:<receptor>` for an off-target stage.
        redone = {
            (int(shard), int(receptor)): str(selection or "")
            for shard, receptor, selection in session.exec(
                select(ScreeningShardRun.shard_id, ScreeningShardRun.receptor_molecule_id,
                       ScreeningShardRun.selection).where(
                    ScreeningShardRun.docking_run_id == current.id,
                    ScreeningShardRun.state == TargetState.DONE,
                )
            ).all()
        }
        older = select(DockingRun.id).where(
            DockingRun.engine == current.engine, DockingRun.run_kind == current.run_kind,
            DockingRun.protocol_hash == current.protocol_hash, DockingRun.id != current.id,
        )

        def covered(row: ScreeningShardRun) -> bool:
            """Did this run dock that receipt's pair again, under a selection holding all of it?"""
            key = (int(row.shard_id), int(row.receptor_molecule_id))
            return key in redone and selection_covers(redone[key], str(row.selection or ""))

        stale = [
            row
            for row in session.exec(
                select(ScreeningShardRun).where(ScreeningShardRun.docking_run_id.in_(older))
            ).all()
            if covered(row)
        ]
        paths = {row.result_path for row in stale if row.result_path}
        touched = {(int(row.docking_run_id), int(row.receptor_molecule_id)) for row in stale}
        # Read each retired fragment before it goes: the shard records it holds are the pairs it
        # was the evidence for, and a library molecule is addressed by (import batch, record).
        batches = {
            int(row.id): row.import_batch_id
            for row in session.exec(select(ScreeningShard).where(
                ScreeningShard.id.in_({int(row.shard_id) for row in stale}))).all()
        } if stale else {}
        retired = {
            (int(row.docking_run_id), int(row.receptor_molecule_id),
             batches.get(int(row.shard_id)), record)
            for row in stale
            for record in fragment_ligand_ids(_at(root, row.result_path))
        }
        if stale:
            session.exec(delete(ScreeningShardRun).where(ScreeningShardRun.id.in_([row.id for row in stale])))
            session.commit()
        emptied = [(run, receptor) for run, receptor in sorted(touched) if session.exec(
            select(ScreeningShardRun.id).where(ScreeningShardRun.docking_run_id == run,
                                               ScreeningShardRun.receptor_molecule_id == receptor).limit(1)
        ).first() is None]
        for run, receptor in emptied:
            session.exec(delete(ScreeningTarget).where(ScreeningTarget.docking_run_id == run,
                                                       ScreeningTarget.receptor_molecule_id == receptor))
        session.commit()
        kept = set(session.exec(select(ScreeningShardRun.result_path).where(
            ScreeningShardRun.result_path.in_(paths))).all()) if paths else set()
        run_id = str(current.run_id)
    if emptied:
        record = DockingResultRecord
        delete_results_where(project_db, or_(*(
            and_(record.docking_run_id == run, record.receptor_molecule_id == receptor)
            for run, receptor in emptied
        )))
    orphaned = _results_of_retired_shards(project_db, retired)
    if orphaned:
        delete_results_where(project_db, DockingResultRecord.id.in_(orphaned))
    supersede_results(project_db, run_id=run_id)
    for value in paths - kept:
        _at(root, value).unlink(missing_ok=True)
    return len(stale)


def _at(root, value: str) -> Path:
    """A receipt's fragment: stored relative to the project, absolute in an older project."""
    path = Path(value)
    return path if path.is_absolute() else Path(root) / path


def _results_of_retired_shards(project_db, retired: set[tuple[int, int, int | None, int]]) -> list[int]:
    """The materialized results whose evidence just went: rows of an older run whose ligand is a
    record of a shard that run no longer has a receipt for, against that same receptor.

    Driven from the results, which a campaign caps, rather than from the shard records, of which
    there are as many as the library holds."""
    pairs = sorted({(run, receptor) for run, receptor, _batch, _record in retired})
    if not pairs:
        return []
    record = DockingResultRecord
    with project_db.get_session() as session:
        rows = session.exec(
            select(record.id, record.docking_run_id, record.receptor_molecule_id,
                   MoleculeRecord.import_batch_id, MoleculeRecord.source_index)
            .join(MoleculeRecord, MoleculeRecord.id == record.ligand_molecule_id)
            .where(or_(*(
                and_(record.docking_run_id == run, record.receptor_molecule_id == receptor)
                for run, receptor in pairs
            )))
        ).all()
    return [
        int(result_id)
        for result_id, run, receptor, batch, source_index in rows
        if (int(run), int(receptor), None if batch is None else int(batch),
            int(source_index or 0)) in retired
    ]
