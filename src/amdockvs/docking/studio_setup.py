"""Docking Studio's setup for a project, read from the docking it submitted.

Clicking Run commits to a set of parameters: that is what the Programs step reopens on, not what
was typed after. What happens to the job afterwards -- waiting in a queue, on a dependency, on a
cluster, cancelled, failed -- does not change what was committed to. MolSuite records every
submitted job in the project's journal (`<project>/executor.db`) when it is submitted, with the
params it was submitted with (`origin`); that record is read here, in submission order. Nothing is
written for this. Settings supply the defaults for a program without docking history.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from amdockvs.docking.engines.programs import list_docking_programs
from amdockvs.docking.planning import click_id
from amdockvs.project.submitted import jobs_from_rows, submitted_jobs

_log = logging.getLogger(__name__)

REDOCKING = "redocking"
#: The MolSuite jobs that submit docking (their `name`), and the experiment each one is.
DOCKING_JOBS = {
    "amdock_docking_job": "docking",
    "amdock_dock_shards_job": "docking",
    "amdock_dock_rows_campaign": "docking",  # on a cluster's queue
    "amdock_dock_shards_campaign": "docking",
    "amdock_redocking_job": REDOCKING,
}


def _program_config(program: str, config: object) -> dict[str, Any] | None:
    """`config` as `program` validates it, cut to the fields it declares; None for a program this
    build does not know or a config it refuses (a record it cannot read says nothing about the
    setup). The editor cuts a variant further, to what its page sets."""
    spec = next((spec for spec in list_docking_programs() if spec.key == program), None)
    if spec is None or not isinstance(config, dict):
        return None
    try:
        validated = spec.validate_config(config, strict=True)
    except (TypeError, ValueError) as error:  # pydantic's ValidationError is a ValueError
        _log.warning("Ignoring a submitted %s configuration this build refuses: %s", program, error)
        return None
    return {name: value for name, value in validated.items() if name in config and name in spec.config_model.model_fields}


def submitted_docking(project_root: str | Path | None) -> list[dict[str, Any]]:
    """Every docking job the project submitted, oldest first, as {kind, run_id, program, config,
    label, rescoring} (project/submitted.py)."""
    return _docking(submitted_jobs(project_root, DOCKING_JOBS))


def submissions(rows: Iterable[dict]) -> list[dict[str, Any]]:
    """The docking among journal `rows` (as `ms_flow.engine.journal.recorded` gives them)."""
    return _docking(jobs_from_rows(rows, DOCKING_JOBS))


def _docking(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for job in jobs:
        params = job["params"]
        protocol = params.get("protocol_metadata") or {}
        if not isinstance(protocol, dict):
            continue
        program = str(protocol.get("program") or params.get("engine") or "")
        config = _program_config(program, protocol.get("config") or params.get("engine_config"))
        rescoring = protocol.get("rescoring") or []
        if config is None or not isinstance(rescoring, list) or not all(isinstance(r, dict) for r in rescoring):
            continue  # each rescoring step is a mapping (planning.DockingProtocol); this is not one
        # A click over sharded libraries gives each program's job a run_id of its own.
        found.append({"kind": DOCKING_JOBS[job["name"]], "run_id": click_id(params.get("run_id")),
                      "job": job["job_id"], "program": program, "config": config,
                      "label": protocol.get("label"), "rescoring": [dict(step) for step in rescoring]})
    return found


def setup_from_submissions(submitted: list[dict[str, Any]]) -> dict[str, Any]:
    """The setup `submitted` (oldest first) commits to, as `ProtocolEditorWidget._apply_docking_setup`
    takes it, or {} when nothing was submitted.

    - experiment: the kind of the latest submission.
    - method, programs: those of the latest Docking click (one click's jobs share a `run_id`).
    - configs: each program's latest Docking submission, whether or not it was in that click.
    - variants: the protocols of the latest Redocking click.
    """
    if not submitted:
        return {}

    def latest_click(kind: str) -> list[dict]:
        mine = [item for item in submitted if item["kind"] == kind]
        if not mine:
            return []
        last = mine[-1]
        if not last["run_id"]:  # submitted without one: a click of its own
            return [last]
        return [item for item in mine if item["run_id"] == last["run_id"]]

    setup: dict[str, Any] = {"experiment": submitted[-1]["kind"]}
    docking = latest_click("docking")
    if docking:
        setup["method"] = str(docking[0]["config"].get("method") or "standard")
        setup["programs"] = list(dict.fromkeys(item["program"] for item in docking))
        # Each program's latest Docking submission: later ones replace earlier ones.
        setup["configs"] = {item["program"]: item["config"] for item in submitted if item["kind"] == "docking"}
    setup["variants"] = [{key: item[key] for key in ("program", "config", "label", "rescoring")}
                         for item in latest_click(REDOCKING)]
    return setup


def setup_from_project(project_root: str | Path | None) -> dict[str, Any]:
    """The setup the project's submitted docking commits to (`setup_from_submissions`)."""
    return setup_from_submissions(submitted_docking(project_root))


__all__ = ["DOCKING_JOBS", "setup_from_project", "setup_from_submissions", "submissions", "submitted_docking"]
