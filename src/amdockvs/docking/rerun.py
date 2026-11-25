"""One current screening configuration per program; redocking keeps its comparisons."""
from __future__ import annotations

from pathlib import Path

from ms_flow.core.data import catalog, states
from sqlalchemy import and_, delete, exists, or_
from sqlmodel import select

from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.results.dataset import DONE_KIND, docked_pairs, result_roles
from amdockvs.docking.results.repository import _decode_metadata, supersede_results
from amdockvs.models import (
    DockingResultRecord, DockingRun, MoleculeRecord, ScreeningTarget,
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
                exists().where(and_(ScreeningTarget.docking_run_id == DockingRun.id,
                                    ScreeningTarget.state_keys != "")),
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
    from amdockvs.screening.campaign import target_keys

    keys = target_keys(project_db, run_ids)
    delete_results_where(project_db, DockingResultRecord.docking_run_id.in_(run_ids))
    with project_db.get_session() as session:
        session.exec(delete(ScreeningTarget).where(ScreeningTarget.docking_run_id.in_(run_ids)))
        session.commit()
    # The key's files and what it had done go together: nothing of it is left to resume from.
    for key in sorted(keys):
        for role in result_roles(key):
            catalog.remove(project_db, role)
        states.forget(project_db, DONE_KIND, key)


def supersede_shard_runs(project_db, *, docking_run_id: int | None, keys: set[str] | None = None) -> int:
    """A forced campaign completed: what it docked again replaces what older runs held for those
    shards, instead of sitting beside it. A read keeps one row per (shard, ligand), the newest; the
    run's finish then retires the older shards its redo covers whole, so this reads them first.

    Within a key (one receptor, one site, one input, one protocol), a shard this run docked
    again retires what an older run of the same key had for it. An off-target stage has a key of
    its own, so it neither replaces nor is replaced by a library docking of its receptor.

    The materialized results those older rows were the evidence for go too: a hit that no longer
    qualifies under the new ranking would otherwise stay behind with nothing supporting it. A
    library molecule is addressed by (import batch, shard record), so a retired shard is a span
    of record ids inside its batch. A receptor an older run is left with no shard at all loses
    whatever results remain there and its plan row with them. A result of a pair the new run
    also materialized goes in any case. Returns how many (older run, shard) pairs were retired."""
    if not docking_run_id:
        return 0
    with project_db.get_session() as session:
        current = session.get(DockingRun, int(docking_run_id))
        if current is None:
            return 0
        rows = session.exec(select(ScreeningTarget.receptor_molecule_id, ScreeningTarget.state_keys)
                            .where(ScreeningTarget.state_keys != "")).all()
    # Which receptor a key is of: its targets say so, in whichever run planned them.
    receptor_of = {key: int(receptor) for receptor, state_keys in rows
                   for key in str(state_keys or "").split(",") if key}
    wanted = set(keys) if keys is not None else set(receptor_of)
    stale: set[tuple[int, int, int]] = set()  # (older run, receptor, shard)
    for key in sorted(wanted & set(receptor_of)):
        pairs = docked_pairs(project_db, [key])
        mine = {shard for run, shard in pairs if run == int(current.id)}
        stale |= {(run, receptor_of[key], shard) for run, shard in pairs
                  if run != int(current.id) and shard in mine}
    return _retire(project_db, current, stale)
def _retire(project_db, current: DockingRun, stale: set[tuple[int, int, int]]) -> int:
    """The materialized results and plan rows of what `stale` ((older run, receptor, shard))
    retires; the rows themselves stay, hidden by the newer ones."""
    if not stale:
        supersede_results(project_db, run_id=str(current.run_id))
        return 0
    # A retired shard is a span of record ids inside its import batch.
    spans = {
        (run, receptor) : []
        for run, receptor, _shard in stale
    }
    for run, receptor, shard in sorted(stale):
        found = catalog.get_shard(project_db, int(shard))
        if found is not None:
            spans[(run, receptor)].append(
                (found.get("scope"), int(found["base_id"]), int(found["base_id"]) + int(found["slot_count"])))
    with project_db.get_session() as session:
        # An older run left without a shard of its own keeps nothing: its plan row goes, and
        # with it whatever results it still had there.
        remaining = {
            (run, receptor)
            for key, receptor in (
                (key, receptor_id)
                for receptor_id, state_keys in session.exec(
                    select(ScreeningTarget.receptor_molecule_id, ScreeningTarget.state_keys)
                    .where(ScreeningTarget.state_keys != "")).all()
                for key in str(state_keys or "").split(",") if key
            )
            for run, shard in docked_pairs(project_db, [key])
            if run != int(current.id) and (run, int(receptor), shard) not in stale
        }
    emptied = sorted({(run, receptor) for run, receptor in spans if (run, receptor) not in remaining})
    if emptied:
        with project_db.get_session() as session:
            for run, receptor in emptied:
                session.exec(delete(ScreeningTarget).where(ScreeningTarget.docking_run_id == run,
                                                           ScreeningTarget.receptor_molecule_id == receptor))
            session.commit()
        record = DockingResultRecord
        delete_results_where(project_db, or_(*(
            and_(record.docking_run_id == run, record.receptor_molecule_id == receptor)
            for run, receptor in emptied
        )))
    orphaned = _results_of_retired_shards(project_db, spans)
    if orphaned:
        delete_results_where(project_db, DockingResultRecord.id.in_(orphaned))
    supersede_results(project_db, run_id=str(current.run_id))
    return len(stale)


def _results_of_retired_shards(project_db, spans: dict[tuple[int, int], list]) -> list[int]:
    """The materialized results whose evidence just went: rows of an older run whose ligand is a
    record of a shard this run docked again, against that same receptor.

    Driven from the results, which a campaign caps, rather than from the shard records, of which
    there are as many as the library holds."""
    pairs = sorted(key for key, members in spans.items() if members)
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
        if any(scope == batch and low <= int(source_index or 0) < high
               for scope, low, high in spans.get((int(run), int(receptor)), ()))
    ]
