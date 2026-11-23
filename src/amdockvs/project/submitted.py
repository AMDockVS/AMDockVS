"""What the project submitted: the params MolSuite recorded for each job when it was submitted.

A tool reopens on what its last Run committed to, not on what was typed after: clicking Run is
the commitment, and what happens to the job afterwards (queued, waiting on another, cancelled,
failed) does not change it. MolSuite writes every job to the project's journal
(`<project>/executor.db`) as it is submitted, with its `origin` -- the job and the params it was
submitted with -- and that record is read here, without owning it, as the job monitor reads it.
Nothing is written for this.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Collection, Iterable

_log = logging.getLogger(__name__)


def jobs_from_rows(rows: Iterable[dict], names: Collection[str]) -> list[dict[str, Any]]:
    """The jobs named `names` among journal `rows` (as `ms_flow.engine.journal.recorded` gives
    them), in submission order, as {name, job_id, params, submitted_at} -- `submitted_at` is the
    journal's `started_at`, stamped when the job is submitted (None when the row has none). A
    record without readable params says nothing about what was submitted, and is left out."""
    found = []
    for row in rows:
        name = str(row.get("name") or "")
        if name not in names:
            continue
        try:
            params = (json.loads(row.get("spec") or "{}").get("origin") or {}).get("params")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(params, dict):
            found.append({"name": name, "job_id": str(row.get("job_id") or ""), "params": params,
                          "submitted_at": row.get("started_at")})
    return found


def submitted_jobs(project_root: str | Path | None, names: Collection[str]) -> list[dict[str, Any]]:
    """The project's submitted jobs named `names`, oldest first (`jobs_from_rows`)."""
    from ms_flow.engine.journal import recorded

    path = Path(project_root) / "executor.db" if project_root is not None else None
    if path is None or not path.is_file():
        return []
    return jobs_from_rows(recorded(path), names)


def last_params(project_root: str | Path | None, names: Collection[str],
                where: Callable[[dict], bool] | None = None) -> dict[str, Any] | None:
    """The params of the project's latest submitted job named `names` (and passing `where`)."""
    for job in reversed(submitted_jobs(project_root, names)):
        if where is None or where(job["params"]):
            return job["params"]
    return None


def project_root(runtime) -> Path | None:
    path = getattr(getattr(runtime, "active_context", None), "path", None)
    return Path(path) if path else None


def restoring(tool: str, restore: Callable[[], None]) -> None:
    """Run a tool's restore; a record it cannot put back is logged and never stops the project
    from opening -- the tool keeps the values it was built with."""
    try:
        restore()
    except Exception:  # noqa: BLE001 - see above
        _log.warning("Could not restore %s from the project's last run", tool, exc_info=True)


__all__ = ["jobs_from_rows", "last_params", "project_root", "restoring", "submitted_jobs"]
