"""Picking where a job runs: HPC is a destination that coexists with local, not a switch.

Three things hold this together. A job declares the *kind* of place it accepts, because an
HPC executor is registered under whatever the user named the worker. The destination list
offers the local slot and every HPC worker, and leaves `thread` out. And the jobs that can
actually leave this machine say so.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ms_flow.core.executor.submission_service import executor_is_supported

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.runtime import AMDockVSRuntime


class _Adapter:
    def __init__(self, backend):
        self.metadata = type("Meta", (), {"backend": backend})()


class _Manager:
    def __init__(self, executors):
        self._executors = executors

    def registered_executors(self):
        return self._executors


def test_a_job_accepts_an_hpc_worker_under_the_name_its_user_gave_it():
    manager = _Manager({"hpc_ucm": _Adapter("hpc"), "compute": _Adapter("loky")})
    supported = ("thread", "compute", "hpc")
    assert executor_is_supported(manager, "compute", supported), "a plain name still matches"
    assert executor_is_supported(manager, "hpc_ucm", supported), "the backend is the fallback"
    assert not executor_is_supported(manager, "hpc_ucm", ("thread", "compute"))
    assert not executor_is_supported(manager, "typo", supported), "an unknown name is not a pass"


def test_the_destination_list_offers_local_and_every_cluster_but_not_thread(monkeypatch):
    runtime = AMDockVSRuntime()
    monkeypatch.setattr(type(runtime), "_require_active_project", lambda self: None)
    runtime.molsuite = type(
        "MS",
        (),
        {
            "get_executor_capability_matrix": staticmethod(
                lambda: {
                    "thread": {"backend": "thread"},
                    "compute": {"backend": "loky"},
                    "hpc_ucm": {"backend": "hpc"},
                    "hpc_bsc": {"backend": "hpc"},
                }
            )
        },
    )()
    assert runtime.run_destinations() == [
        ("compute", "Local"),
        ("hpc_bsc", "hpc_bsc (HPC)"),
        ("hpc_ucm", "hpc_ucm (HPC)"),
    ]


def test_the_jobs_that_can_leave_this_machine_say_so():
    """A worker that opens project.db cannot run on a cluster, so this list is deliberate."""
    import amdockvs.chemistry.jobs  # noqa: F401
    import amdockvs.docking.jobs  # noqa: F401
    import amdockvs.docking.preparation.jobs as preparation
    import amdockvs.docking.shard_jobs as shard_jobs

    assert "hpc" in AMDOCKVS_OFFLOADABLE_EXECUTORS
    assert "hpc" in shard_jobs.DockShardsJobSpec.supported_executors
    assert "hpc" in preparation.PrepareLigandShardsJobSpec.supported_executors
