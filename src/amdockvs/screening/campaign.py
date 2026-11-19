"""What a running campaign looks like from outside: its receptors, and how far each one is.

`docking_results` cannot answer this. In ranked mode it is empty until the run ends, and even
in streaming mode a receptor that has not produced a hit yet has no row anywhere — so the
Results view shows nothing while thousands of dockings are in flight. The plan is written when
the run is submitted and the counters move once per returned chunk, both in the parent.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from sqlmodel import select

from amdockvs.models import DockingRun, ScreeningShardRun, ScreeningTarget
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

    One row per receptor, whatever its sites: progress is the receptor's, summed over them."""
    rows: dict[int, dict[str, Any]] = {}
    for receptor in receptors:
        receptor_id = int(receptor.get("receptor_id") or 0)
        merged = rows.setdefault(receptor_id, {})
        for key in ("receptor_name", "ligands_total"):
            if receptor.get(key) is not None:
                merged[key] = receptor[key]
        if receptor.get("ligands_done") is not None:
            merged["ligands_done"] = int(merged.get("ligands_done") or 0) + int(receptor["ligands_done"])
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


def advance_shard_run(
    project_db,
    *,
    docking_run_id: int,
    shard_id: int,
    receptor_id: int,
    engine: str,
    protocol_hash: str,
    scored: int,
    hits: int,
    binding_site_id: int = 0,
    result_path: str = "",
    selection: str = "",
    input_key: str = "",
) -> bool:
    """Persist one pair receipt. Returns false when that exact result was already recorded.

    `selection` records which ligands of the shard the receipt stands for (empty: all of them),
    so a forced re-dock replaces only the receipts its own selection covers."""
    with project_db.get_session() as session:
        row = session.exec(
            select(ScreeningShardRun).where(
                ScreeningShardRun.docking_run_id == int(docking_run_id),
                ScreeningShardRun.shard_id == int(shard_id),
                ScreeningShardRun.receptor_molecule_id == int(receptor_id),
                ScreeningShardRun.binding_site_id == int(binding_site_id or 0),
            )
        ).first()
        if row is not None and row.state == TargetState.DONE:
            return False
        row = row or ScreeningShardRun(
            docking_run_id=int(docking_run_id),
            shard_id=int(shard_id),
            receptor_molecule_id=int(receptor_id),
            binding_site_id=int(binding_site_id or 0),
            engine=str(engine),
            protocol_hash=str(protocol_hash or ""),
        )
        row.ligands_scored = max(0, int(scored))
        row.hits = max(0, int(hits))
        row.result_path = str(result_path or "")
        row.selection = str(selection or "")
        row.input_key = str(input_key or "")
        row.state = TargetState.DONE
        session.add(row)
        session.commit()
        return True


def completed_shard_targets(project_db, *, docking_run_id: int | None,
                            selection: str = "") -> set[tuple[int, int, int]]:
    """Completed ``(shard_id, receptor_id, binding_site_id)`` triples of one docking run (one
    engine and protocol), among the receipts docked under `selection` (empty: the whole shard).

    The site is part of the key: a receptor docked in three of its sites is three targets."""
    if not docking_run_id:
        return set()
    with project_db.get_session() as session:
        return {
            (int(shard_id), int(receptor_id), int(site_id or 0))
            for shard_id, receptor_id, site_id in session.exec(
                select(ScreeningShardRun.shard_id, ScreeningShardRun.receptor_molecule_id,
                       ScreeningShardRun.binding_site_id).where(
                    ScreeningShardRun.docking_run_id == int(docking_run_id),
                    ScreeningShardRun.selection == str(selection or ""),
                    ScreeningShardRun.state == TargetState.DONE,
                )
            ).all()
        }


def completed_protocol_targets(project_db, *, engine: str, protocol_hash: str, selection: str = "",
                               input_keys: Mapping[tuple[int, int], str] | None = None,
                               ) -> set[tuple[int, int, int]]:
    """Completed ``(shard_id, receptor_id, binding_site_id)`` triples of every screening run of
    this engine and protocol: what "skip pairs already docked" leaves out, whichever run docked them. A
    campaign's run id is new on every submission, so a skip keyed by it skips nothing.

    Only the receipts docked under `selection` count. A receipt of an off-target stage stands
    for one reference receptor's Top-N inside the shard, not the shard: counting it would make
    a full-library run skip the records it never docked.

    With `input_keys` ((receptor id, site id) -> its current `docking_input_key`), only receipts
    docked against those inputs count: a receptor prepared again or given a new box is docked
    again."""
    with project_db.get_session() as session:
        return {
            (int(shard_id), int(receptor_id), int(site_id or 0))
            for shard_id, receptor_id, site_id, key in session.exec(
                select(ScreeningShardRun.shard_id, ScreeningShardRun.receptor_molecule_id,
                       ScreeningShardRun.binding_site_id, ScreeningShardRun.input_key)
                .join(DockingRun, DockingRun.id == ScreeningShardRun.docking_run_id)
                .where(
                    DockingRun.engine == str(engine).strip().lower(),
                    DockingRun.run_kind == "screening",
                    DockingRun.protocol_hash == str(protocol_hash or ""),
                    ScreeningShardRun.selection == str(selection or ""),
                    ScreeningShardRun.state == TargetState.DONE,
                )
            ).all()
            if input_keys is None or key == input_keys.get((int(receptor_id), int(site_id or 0)))
        }


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
            }
            for row, submit_id in rows
        ]


__all__ = [
    "advance_shard_run",
    "advance_target",
    "completed_protocol_targets",
    "completed_shard_targets",
    "finish_run",
    "list_targets",
    "plan_targets",
    "stop_run",
]
