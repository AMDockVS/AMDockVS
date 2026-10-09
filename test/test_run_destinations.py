"""Where a job runs: one destination, and the jobs that could leave this machine say so.

Who runs the jobs of this machine (the local pool, Ray or a cluster's queue) is the
`resources.process` setting of ms_flow, so there is nothing to pick for each job.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.runtime import AMDockVSRuntime


def test_the_destination_list_offers_local_only(monkeypatch):
    runtime = AMDockVSRuntime()
    monkeypatch.setattr(type(runtime), "_require_active_project", lambda self: None)
    assert runtime.run_destinations() == [("compute", "Local")]


def test_the_jobs_that_can_leave_this_machine_say_so():
    """A worker that opens project.db cannot run on a cluster, so this list is deliberate."""
    import amdockvs.chemistry.jobs  # noqa: F401
    import amdockvs.docking.jobs  # noqa: F401
    import amdockvs.docking.preparation.jobs as preparation
    import amdockvs.docking.shard_jobs as shard_jobs

    assert "hpc" in AMDOCKVS_OFFLOADABLE_EXECUTORS
    assert "hpc" in shard_jobs.DockShardsJobSpec.supported_executors
    assert "hpc" in preparation.PrepareLigandShardsJobSpec.supported_executors
