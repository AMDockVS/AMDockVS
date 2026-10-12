"""Where a job runs: one destination, and the jobs that could leave this machine say so.

The pool of this machine for now: the executors kept in the settings of ms_flow are
not offered for each job yet.
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
