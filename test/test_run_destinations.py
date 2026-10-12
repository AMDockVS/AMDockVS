"""Where a job runs: one of the executors that are active, and only the jobs that can leave
this machine are sent to one that has other machines."""
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.runtime import AMDockVSRuntime


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
    import amdockvs.docking.jobs  # noqa: F401
    import amdockvs.docking.preparation.jobs as preparation
    import amdockvs.docking.shard_jobs as shard_jobs

    assert "hpc" in AMDOCKVS_OFFLOADABLE_EXECUTORS
    assert "hpc" in shard_jobs.DockShardsJobSpec.supported_executors
    assert "hpc" in preparation.PrepareLigandShardsJobSpec.supported_executors
    # What chemistry writes is named relative to the project: a far disk does not give it back.
    assert "hpc" not in chemistry.ligand_chemistry_job.supported_executors
    assert "hpc" not in chemistry.receptor_chemistry_job.supported_executors


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
