"""No job writes rows through a path of its own: it either declares a sink or is whitelisted.

Without this, a new job can persist from `finalize` with its own session and nobody notices —
which is exactly how `amdock_pocket_prediction_job` ended up before it became additive. The test
does not guess whether a job writes: it forces every exception to be explicit and argued here.
"""
from __future__ import annotations

import importlib
import pkgutil

import amdockvs
from ms_flow.tasking import JobDefinition

# Why each one has no sink. If one gains one, this test fails and its line must be deleted.
WITHOUT_SINK = {
    "amdock_diagram_job": "writes no rows: it leaves PNG/SVG on disk.",
}


def _job_definitions() -> dict[str, JobDefinition]:
    found: dict[str, JobDefinition] = {}
    for module in pkgutil.walk_packages(amdockvs.__path__, "amdockvs."):
        imported = importlib.import_module(module.name)
        for value in vars(imported).values():
            if isinstance(value, JobDefinition):
                found[value.name] = value
    return found


def test_every_job_declares_a_sink_or_is_whitelisted():
    jobs = _job_definitions()
    assert jobs, "no JobDefinition was found: the module walk is broken."
    assert {name for name, job in jobs.items() if job.output_spec is None} == set(WITHOUT_SINK)
