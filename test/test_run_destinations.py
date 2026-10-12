"""Where a job runs: one of the executors that are active, and only the jobs that can leave
this machine are sent to one that has other machines."""
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.core.jobs import job, task
from amdockvs.runtime import AMDockVSRuntime


@task()
def _nap(payload: dict) -> list:
    time.sleep(0.02)
    return []


@job(task=_nap, name="napping")
def napping(params: dict, config: dict):
    assert config["project_db"] is not None
    yield from ({"n": n} for n in range(int(params["naps"])))


def test_a_job_goes_to_an_active_executor_it_can_run_on(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ms_flow import engine as engines
    from ms_flow import ray_hosts

    class Ray:
        capacity, uses_tokens = 1, False

        def __init__(self, *said, **init):
            pass

        def bind(self, on_done):
            pass

        def shutdown(self):
            pass

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ray_hosts, "RayOverHosts", Ray)
    monkeypatch.setattr(engines, "RayExecutor", Ray)
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="p", folder=tmp_path / "p", description="")
        ms = runtime.workspace
        ms.save_host("laptop", address="192.168.1.2", env="/env")
        ms.save_executor("far", kind="ray", hosts=["laptop"])
        ms.save_executor("alone", kind="ray", hosts=["local"])
        here = SimpleNamespace(name="import", executor="compute", supported_executors=("thread", "compute"))
        leaves = SimpleNamespace(name="dock", executor="compute", supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS)

        assert runtime.run_destinations() == [("local", "local")]  # nothing is started at start-up
        assert runtime._run_on(here, None) == runtime._run_on(leaves, "compute") == "local"
        assert runtime._run_on(SimpleNamespace(executor="thread"), None) == runtime._run_on(leaves, "thread") == "thread"
        with pytest.raises(ValueError, match="`dock` is not sent to `far`: it is not active"):
            runtime._run_on(leaves, "far")

        ms.activate("far")
        ms.settings_manager.set_default_executor("far")
        assert runtime.run_destinations() == [("far", "far"), ("local", "local")]  # the default one first
        assert runtime.run_destinations(here) == [("local", "local")]
        assert (runtime._run_on(leaves, None), runtime._run_on(here, None)) == ("far", "local")
        with pytest.raises(ValueError, match="`import` is not sent to `far`: its worker runs on this machine only"):
            runtime._run_on(here, "far")

        ms.deactivate("far")
        ms.activate("alone")  # a Ray of this machine: its pool is aside, and every job goes to it
        assert runtime.run_destinations(here) == [("alone", "alone")] and runtime._run_on(here, "compute") == "alone"
        ms.deactivate("alone")
        ms.deactivate("local")
        with pytest.raises(RuntimeError, match="no active executor runs `import` now"):
            runtime._run_on(here, None)
    finally:
        runtime.shutdown()


def test_the_jobs_that_can_leave_this_machine_say_so():
    """A worker that opens project.db cannot run on a cluster, so this list is deliberate."""
    import amdockvs.chemistry.jobs as chemistry
    import amdockvs.diversity.jobs as diversity
    import amdockvs.docking.jobs  # noqa: F401
    import amdockvs.docking.preparation.jobs as preparation
    import amdockvs.docking.shard_jobs as shard_jobs
    import amdockvs.qsar.jobs as qsar

    assert "hpc" in AMDOCKVS_OFFLOADABLE_EXECUTORS
    assert "hpc" in shard_jobs.DockShardsJobSpec.supported_executors
    assert "hpc" in preparation.PrepareLigandShardsJobSpec.supported_executors
    assert "hpc" in chemistry.ligand_chemistry_job.supported_executors
    assert "hpc" in chemistry.receptor_chemistry_job.supported_executors
    assert "hpc" in diversity.cluster_molecules_job.supported_executors
    assert "hpc" in qsar.calculate_molecule_descriptors_job.supported_executors


def test_the_files_a_chunk_names_whole_are_kept_relative_to_the_project(tmp_path):
    """A chunk on another disk names what it wrote by its whole path, or the file stays there."""
    from amdockvs.core.jobs import _Relative

    class Sink:
        tables, kept = ("molecules",), []

        def write(self, results):
            self.kept.extend(results)

    writer = _Relative(Sink(), tmp_path)
    writer.write([{"molecules": [{"id": 1, "current_path": str(tmp_path / "data/a.sdf"), "source": "/elsewhere/b.sdf"}]}])
    assert Sink.kept == [{"molecules": [{"id": 1, "current_path": "data/a.sdf", "source": "/elsewhere/b.sdf"}]}]
    assert writer.tables == ("molecules",) and not hasattr(writer, "finish")


def test_the_executors_are_started_from_the_bar_and_kept_in_settings(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui import AMDockVSMainWindow

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window, runtime = None, AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_project", folder=tmp_path / "project", description="ui test")
        window = AMDockVSMainWindow(runtime=runtime)
        window.views.executors_action.trigger()
        panel = window.executors_panel
        assert panel.isVisible() and panel.states["local"].text() == "On" and window.open_executors_panel() is panel
        panel.settings_button.click()
        app.processEvents()
        settings = window._app_widget._settings_dialog.panel
        assert settings.stack.currentWidget().names.item(0).text() == "local"  # the page of the executors
        assert {"page:Hosts", "page:Executors"} <= set(settings._pages)
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


def test_a_job_on_record_is_continued_and_one_that_runs_is_moved(tmp_path, monkeypatch):
    from ms_flow.engine.flow import named

    import amdockvs.docking.jobs as docking

    for declared in (napping, docking.docking_job, docking.DockingJobSpec.definition):
        engine = declared.engine
        assert named(f"{engine.__module__}:{engine.__qualname__}") is engine and engine.definition is declared

    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="p", folder=tmp_path / "p", description="")
        status = runtime.workspace.engine.status
        runtime.workspace.save_host("laptop", address="192.168.1.2", env="/env")
        runtime.workspace.save_executor("far", kind="ray", hosts=["laptop"])
        job_id = runtime.submit_job(napping, params={"naps": 60}, executor_name="thread", cpu_required=2)
        first = runtime._engine_ids()[job_id]
        while status(first).chunks_done < 5:
            time.sleep(0.01)
        assert runtime.job_destinations(job_id) == [("local", "local")] and runtime.job_destinations("nobody") == []
        assert runtime.create_monitor_bridge().job_destinations(job_id) == ["local"]  # what the monitor offers
        with pytest.raises(ValueError, match="`napping` is not sent to `far`"):
            runtime.resume_job(job_id, "far")
        assert status(first).status == "running"  # refused before anything was canceled

        assert runtime.resume_job(job_id, "thread") == job_id  # moved while it runs: canceled and continued
        assert status(first).status == "canceled" and runtime._engine_ids()[job_id] != first
        ended = runtime.wait_for_job(job_id)
        assert (ended.status, status(runtime._engine_ids()[job_id]).chunks_done) == ("completed", 60)
        assert runtime.workspace.origin(job_id)["options"]["cpu"] == 2

        # A docking job has thousands of tasks and each one says how far it is: watched all the same.
        assert docking.DockingJobSpec.definition.task_progress and not napping.task_progress
        said, submit = [], runtime.workspace.submit
        monkeypatch.setattr(runtime.workspace, "submit", lambda *job, **options: said.append(options) or submit(*job, **options))
        waits = docking.DockingJobSpec.definition.on_thread  # and one that only waits for a program has no worker
        assert waits({"engine": "qvina"}) and waits({"engine": "vina", "vina_backend": "binary"})
        assert not waits({"engine": "vina", "vina_backend": "python"}) and not waits({"engine": "gnina"})
        for declared in (napping.with_options(task_progress=True, on_thread=lambda params: params["naps"] == 1), napping):
            runtime.wait_for_job(runtime.submit_job(declared, params={"naps": 1}, executor_name="thread"))
        assert [(options.get("progress"), options.get("thread")) for options in said] == [(True, True), (None, None)]

        runtime.cancel_job(again := runtime.submit_job(napping, params={"naps": 60}, executor_name="thread"))
        assert runtime.wait_for_job(again).status == "canceled"
        runtime.resume_job(again)  # where it was
        assert runtime.wait_for_job(again).status == "completed"
    finally:
        runtime.shutdown()
