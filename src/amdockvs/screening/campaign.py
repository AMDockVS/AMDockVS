"""What a running campaign looks like from outside: its receptors, and how far each one is.

`docking_results` cannot answer this. In ranked mode it is empty until the run ends, and even
in streaming mode a receptor that has not produced a hit yet has no row anywhere — so the
Results view shows nothing while thousands of dockings are in flight. The plan is written when
the run is submitted and the counters move once per returned chunk, both in the parent.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping

from sqlmodel import select

from amdockvs.models import ScreeningShardRun, ScreeningTarget
from amdockvs.core.vocab import TargetState


def plan_targets(
    project_db,
    *,
    run_id: str,
    receptors: Iterable[Mapping[str, Any]],
    ligands_total: int,
    engine: str = "",
) -> int:
    """One row per scheduled receptor. Idempotent: re-submitting a run_id updates its plan."""
    rows = list(receptors)
    if not rows:
        return 0
    with project_db.get_session() as session:
        for receptor in rows:
            receptor_id = int(receptor.get("receptor_id") or 0)
            existing = session.exec(
                select(ScreeningTarget).where(
                    ScreeningTarget.run_id == str(run_id),
                    ScreeningTarget.receptor_molecule_id == receptor_id,
                )
            ).first() or ScreeningTarget(run_id=str(run_id), receptor_molecule_id=receptor_id)
            existing.receptor_name = str(receptor.get("receptor_name") or existing.receptor_name)
            existing.engine = str(engine or existing.engine)
            existing.ligands_total = int(ligands_total)
            existing.state = TargetState.SCHEDULED
            existing.updated_at = datetime.now()
            session.add(existing)
        session.commit()
    return len(rows)


def advance_target(project_db, *, run_id: str, receptor_id: int, scored: int, hits: int) -> None:
    """One chunk came back for this receptor. Additive, so a crash-resume cannot double-count
    what it never wrote."""
    with project_db.get_session() as session:
        row = session.exec(
            select(ScreeningTarget).where(
                ScreeningTarget.run_id == str(run_id),
                ScreeningTarget.receptor_molecule_id == int(receptor_id),
            )
        ).first()
        if row is None:
            return
        row.ligands_done += max(0, int(scored))
        row.hits += max(0, int(hits))
        row.state = TargetState.RUNNING
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()


def finish_run(project_db, *, run_id: str) -> None:
    """The campaign closed: every receptor of this run is done, however far it got."""
    with project_db.get_session() as session:
        for row in session.exec(select(ScreeningTarget).where(ScreeningTarget.run_id == str(run_id))).all():
            row.state = TargetState.DONE
            row.updated_at = datetime.now()
            session.add(row)
        session.commit()


def stop_run(project_db, *, run_id: str, state: str) -> None:
    """Expose a terminal job failure/cancellation in the campaign rows."""
    if state not in (TargetState.CANCELED, TargetState.FAILED):
        raise ValueError(f"Unsupported stopped campaign state: {state}")
    with project_db.get_session() as session:
        for row in session.exec(select(ScreeningTarget).where(ScreeningTarget.run_id == str(run_id))).all():
            if row.state not in (TargetState.DONE, TargetState.FAILED, TargetState.CANCELED):
                row.state = state
                row.updated_at = datetime.now()
                session.add(row)
        session.commit()


def advance_shard_run(
    project_db,
    *,
    run_id: str,
    shard_id: int,
    receptor_id: int,
    engine: str,
    protocol_hash: str,
    scored: int,
    hits: int,
    result_path: str = "",
) -> bool:
    """Persist one pair receipt. Returns false when that exact result was already recorded."""
    with project_db.get_session() as session:
        row = session.exec(
            select(ScreeningShardRun).where(
                ScreeningShardRun.run_id == str(run_id),
                ScreeningShardRun.shard_id == int(shard_id),
                ScreeningShardRun.receptor_molecule_id == int(receptor_id),
                ScreeningShardRun.engine == str(engine),
                ScreeningShardRun.protocol_hash == str(protocol_hash or ""),
            )
        ).first()
        if row is not None and row.state == TargetState.DONE:
            return False
        row = row or ScreeningShardRun(
            run_id=str(run_id),
            shard_id=int(shard_id),
            receptor_molecule_id=int(receptor_id),
            engine=str(engine),
            protocol_hash=str(protocol_hash or ""),
        )
        row.ligands_scored = max(0, int(scored))
        row.hits = max(0, int(hits))
        row.result_path = str(result_path or "")
        row.state = TargetState.DONE
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        return True


def completed_shard_targets(
    project_db,
    *,
    run_id: str,
    engine: str,
    protocol_hash: str,
) -> set[tuple[int, int]]:
    """Completed ``(shard_id, receptor_id)`` pairs for exactly one run/protocol."""
    if not str(run_id or ""):
        return set()
    with project_db.get_session() as session:
        return {
            (int(shard_id), int(receptor_id))
            for shard_id, receptor_id in session.exec(
                select(ScreeningShardRun.shard_id, ScreeningShardRun.receptor_molecule_id).where(
                    ScreeningShardRun.run_id == str(run_id),
                    ScreeningShardRun.engine == str(engine),
                    ScreeningShardRun.protocol_hash == str(protocol_hash or ""),
                    ScreeningShardRun.state == TargetState.DONE,
                )
            ).all()
        }


def list_targets(project_db, *, run_id: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
    """The campaign as rows for a table: receptor, ligands done of total, hits."""
    statement = select(ScreeningTarget)
    if run_id:
        statement = statement.where(ScreeningTarget.run_id == str(run_id))
    if active_only:
        statement = statement.where(ScreeningTarget.state.in_((TargetState.SCHEDULED, TargetState.RUNNING)))
    with project_db.get_session() as session:
        rows = session.exec(statement.order_by(ScreeningTarget.id)).all()
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "receptor_molecule_id": row.receptor_molecule_id,
                "receptor_name": row.receptor_name,
                "engine": row.engine,
                "ligands_total": row.ligands_total,
                "ligands_done": row.ligands_done,
                "pending": max(0, int(row.ligands_total) - int(row.ligands_done)),
                "hits": row.hits,
                "state": row.state,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


def live_target_progress(
    targets: Iterable[Mapping[str, Any]],
    chunks: Iterable[Mapping[str, Any]],
) -> dict[int, dict[str, int]]:
    """Overlay in-flight MF chunk progress on the latest persisted target per receptor.

    Completed chunks are already included in ``ligands_done`` by the result handler. Only
    pending/running/staging chunks arrive here, so the overlay neither writes per-ligand DB
    rows nor double-counts completed work.
    """
    latest: dict[int, Mapping[str, Any]] = {}
    for target in targets:
        receptor_id = int(target.get("receptor_molecule_id") or 0)
        if receptor_id and int(target.get("id") or 0) >= int(latest.get(receptor_id, {}).get("id") or 0):
            latest[receptor_id] = target

    in_flight: dict[tuple[str, int], int] = {}
    for chunk in chunks:
        raw = chunk.get("payload_json") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        receptor_id = int(payload.get("receptor_id") or 0)
        run_id = str(payload.get("run_id") or "")
        target = latest.get(receptor_id)
        if target is None or str(target.get("run_id") or "") != run_id:
            continue
        units = sum(max(0, int(shard.get("n_records") or 0)) for shard in payload.get("shards") or ())
        progress = min(100.0, max(0.0, float(chunk.get("progress") or 0.0)))
        key = (run_id, receptor_id)
        in_flight[key] = in_flight.get(key, 0) + round(units * progress / 100.0)

    result: dict[int, dict[str, int]] = {}
    for receptor_id, target in latest.items():
        total = max(0, int(target.get("ligands_total") or 0))
        persisted = max(0, int(target.get("ligands_done") or 0))
        done = min(total, persisted + in_flight.get((str(target.get("run_id") or ""), receptor_id), 0))
        result[receptor_id] = {"ligands": total, "docked": done, "pending": max(0, total - done)}
    return result


__all__ = [
    "advance_shard_run",
    "advance_target",
    "completed_shard_targets",
    "finish_run",
    "list_targets",
    "live_target_progress",
    "plan_targets",
    "stop_run",
]
