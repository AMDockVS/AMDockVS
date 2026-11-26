"""What a running campaign looks like from outside: its receptors, and how far each one is.

`docking_results` cannot answer this. In ranked mode it is empty until the run ends, and even
in streaming mode a receptor that has not produced a hit yet has no row anywhere — so the
Results view shows nothing while thousands of dockings are in flight. The plan is written when
the run is submitted and the counters move once per returned chunk, both in the parent.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from pathlib import Path

from ms_flow.core.data import catalog, states
from sqlmodel import select

from amdockvs.docking.results.dataset import DONE_KIND, payload_part, result_roles
from amdockvs.models import DockingRun, ScreeningTarget
from amdockvs.core.vocab import TargetState


def plan_targets(
    project_db,
    *,
    docking_run_id: int,
    receptors: Iterable[Mapping[str, Any]],
    ligands_total: int,
    engine: str = "",
) -> int:
    """One row per scheduled receptor. Idempotent: re-planning a run updates its plan.

    A receptor may bring `ligands_done`: how many of the run's OWN ligands were already docked
    when it was planned (what "Skip pairs already docked" takes out of its work). Docked counts
    from there, so it speaks of the selection and of nothing else. It may also bring its own
    `ligands_total`, which is what a receptor docked in several of its sites needs: its work is
    the library once per site.

    One row per receptor, whatever its sites: progress is the receptor's, summed over them, and
    `state_keys` names the key of each of them (`state_key`), which is where its results are."""
    rows: dict[int, dict[str, Any]] = {}
    for receptor in receptors:
        receptor_id = int(receptor.get("receptor_id") or 0)
        merged = rows.setdefault(receptor_id, {})
        for key in ("receptor_name", "ligands_total"):
            if receptor.get(key) is not None:
                merged[key] = receptor[key]
        if receptor.get("ligands_done") is not None:
            merged["ligands_done"] = int(merged.get("ligands_done") or 0) + int(receptor["ligands_done"])
        if receptor.get("state_key"):
            merged.setdefault("state_keys", set()).add(str(receptor["state_key"]))
    if not rows:
        return 0
    with project_db.get_session() as session:
        for receptor_id, receptor in rows.items():
            existing = session.exec(
                select(ScreeningTarget).where(
                    ScreeningTarget.docking_run_id == int(docking_run_id),
                    ScreeningTarget.receptor_molecule_id == receptor_id,
                )
            ).first() or ScreeningTarget(docking_run_id=int(docking_run_id), receptor_molecule_id=receptor_id)
            existing.receptor_name = str(receptor.get("receptor_name") or existing.receptor_name)
            existing.engine = str(engine or existing.engine)
            existing.ligands_total = int(receptor.get("ligands_total") or ligands_total)
            if receptor.get("ligands_done") is not None:
                existing.ligands_done = int(receptor["ligands_done"])
            if receptor.get("state_keys"):
                existing.state_keys = ",".join(sorted(receptor["state_keys"] | set(filter(None, existing.state_keys.split(",")))))
            existing.state = TargetState.SCHEDULED
            session.add(existing)
        session.commit()
    return len(rows)


def advance_target(project_db, *, docking_run_id: int, receptor_id: int, scored: int = 0, hits: int = 0,
                   imported: int = 0) -> None:
    """One chunk came back for this receptor. Additive, so a crash-resume cannot double-count
    what it never wrote. `imported` alone is the top-N landing as rows at the end: no state change."""
    with project_db.get_session() as session:
        row = session.exec(
            select(ScreeningTarget).where(
                ScreeningTarget.docking_run_id == int(docking_run_id),
                ScreeningTarget.receptor_molecule_id == int(receptor_id),
            )
        ).first()
        if row is None:
            return
        row.ligands_done += max(0, int(scored))
        row.hits += max(0, int(hits))
        row.imported += max(0, int(imported))
        if scored or hits:
            row.state = TargetState.RUNNING
        session.add(row)
        session.commit()


def finish_run(project_db, *, docking_run_id: int | None) -> None:
    """The campaign closed: every receptor of this run is done, however far it got."""
    with project_db.get_session() as session:
        for row in session.exec(select(ScreeningTarget).where(ScreeningTarget.docking_run_id == docking_run_id)).all():
            row.state = TargetState.DONE
            session.add(row)
        session.commit()


def reopen_run(project_db, *, docking_run_id: int | None) -> None:
    """A stopped run submitted again (Resume): its receptors that stopped are scheduled again."""
    with project_db.get_session() as session:
        for row in session.exec(select(ScreeningTarget).where(
                ScreeningTarget.docking_run_id == docking_run_id,
                ScreeningTarget.state.in_((TargetState.CANCELED, TargetState.FAILED)))).all():
            row.state = TargetState.SCHEDULED
            session.add(row)
        session.commit()


def stop_run(project_db, *, docking_run_id: int | None, state: str) -> None:
    """Expose a terminal job failure/cancellation in the campaign rows."""
    if state not in (TargetState.CANCELED, TargetState.FAILED):
        raise ValueError(f"Unsupported stopped campaign state: {state}")
    with project_db.get_session() as session:
        for row in session.exec(select(ScreeningTarget).where(ScreeningTarget.docking_run_id == docking_run_id)).all():
            if row.state not in (TargetState.DONE, TargetState.FAILED, TargetState.CANCELED):
                row.state = state
                session.add(row)
        session.commit()


def done_shards(project_db, key: str, *, narrowed_by: str = "") -> set[int]:
    """The shards docked for `key`: whole, or — with `narrowed_by` (`Exclusions.key`) — under
    those cuts too, which is what a shard the same cuts leave partial may skip."""
    return {int(s) for s in states.done(project_db, DONE_KIND, key, narrowed_by=narrowed_by or None)}


def record_docked(project_db, *, key: str, shard_id: int, result_path: str = "", part: str = "",
                  narrowed_by: str = "", generation_id: int | None = None, force: bool = False,
                  writer=None, done_ids=None, failed_ids=None) -> bool:
    """One shard docked for `key`: its two result parts (`result_path`, the summary, and the payload
    beside it) committed into the key's containers, then the shard into the done-set (that order: a
    crash in between docks it again, and `part` — the run's id of this task — makes the commit of a
    replay a no-op). False when it was already recorded — a replay — and nothing is written; `force`
    (a forced re-dock, whose shards are done already) records it anyway. `writer`: the parent's
    `PairWriter` of the key, kept open over a run; one of its own (the parts' folder's parent) without.
    `done_ids`/`failed_ids`: the shard's ligands docked and not, as the worker named them; without
    them the shard counts done with unknown ligand counts.

    ponytail: the done-set is read whole per call (one parquet of shard ids per key, ~4k per
    million records); a set kept in the writer if a campaign of millions of shards shows it."""
    from uuid import uuid4

    path = Path(str(result_path or ""))
    if not force and int(shard_id) in done_shards(project_db, key, narrowed_by=narrowed_by):
        if str(result_path or ""):  # a replay's parts: never committed, never read
            from ms_flow.core.data.companions import whole

            for name in [*whole(path), *whole(payload_part(path))]:
                Path(name).unlink(missing_ok=True)
        return False
    if str(result_path or "") and path.is_file():
        part = str(part or uuid4().hex)
        own = writer is None
        if own:
            writer = catalog.PairWriter(project_db, path.parent.parent, generation_id=generation_id,
                                        roles=result_roles(key), prefix=part)
        try:
            (receipt,) = writer.commit_parts([{"part": part, "path": str(path), "bulk": str(payload_part(path))}])
        finally:
            if own:
                writer.close()
        if "error" in receipt:
            raise RuntimeError(receipt["error"])
    records = None if done_ids is None and failed_ids is None else {
        int(shard_id): (list(done_ids or ()), list(failed_ids or ()))}
    states.mark_done(project_db, DONE_KIND, key, [int(shard_id)], narrowed_by=str(narrowed_by or ""),
                     generation_id=generation_id, records=records)
    return True


def target_keys(project_db, docking_run_ids: Iterable[int], *, receptor_id: int | None = None) -> set[str]:
    """The keys the targets of these runs docked under (of one receptor, with `receptor_id`)."""
    statement = select(ScreeningTarget.state_keys).where(
        ScreeningTarget.docking_run_id.in_([int(run) for run in docking_run_ids]))
    if receptor_id is not None:
        statement = statement.where(ScreeningTarget.receptor_molecule_id == int(receptor_id))
    with project_db.get_session() as session:
        return {key for keys in session.exec(statement).all() for key in str(keys or "").split(",") if key}


def list_targets(project_db, *, run_id: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
    """The campaign as rows for a table: receptor, ligands done of total, hits."""
    statement = select(ScreeningTarget, DockingRun.run_id).join(DockingRun, DockingRun.id == ScreeningTarget.docking_run_id)
    if run_id:
        statement = statement.where(DockingRun.run_id == str(run_id))
    if active_only:
        statement = statement.where(ScreeningTarget.state.in_((TargetState.SCHEDULED, TargetState.RUNNING)))
    with project_db.get_session() as session:
        rows = session.exec(statement.order_by(ScreeningTarget.id)).all()
        return [
            {
                "id": row.id,
                "run_id": submit_id,
                "docking_run_id": row.docking_run_id,
                "receptor_molecule_id": row.receptor_molecule_id,
                "receptor_name": row.receptor_name,
                "engine": row.engine,
                "ligands_total": row.ligands_total,
                "ligands_done": row.ligands_done,
                "pending": max(0, int(row.ligands_total) - int(row.ligands_done)),
                "hits": row.hits,
                "imported": row.imported,
                "state": row.state,
                "state_keys": row.state_keys,
            }
            for row, submit_id in rows
        ]


__all__ = [
    "advance_target",
    "done_shards",
    "finish_run",
    "list_targets",
    "plan_targets",
    "record_docked",
    "stop_run",
    "target_keys",
]
