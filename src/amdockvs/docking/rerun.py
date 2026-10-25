"""One current screening configuration per program; redocking keeps its comparisons."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, exists, or_
from sqlmodel import select

from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.results.repository import _decode_metadata
from amdockvs.models import DockingResultRecord, DockingRun, ScreeningShardRun, ScreeningTarget
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


def check_protocol_conflict(project_db, *, program, protocol: dict, replace_existing: bool) -> bool:
    runs = _program_runs(project_db, program)
    # Older projects hashed fewer fields; an unchanged saved configuration can still resume.
    if runs and len({run.protocol_hash for run in runs}) == 1:
        saved = _decode_metadata(runs[0].protocol)
        if saved.get("config") and resolved_protocol(
            program, program.validate_config(saved["config"]), saved,
        )["hash"] == protocol["hash"]:
            protocol["hash"] = runs[0].protocol_hash
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
