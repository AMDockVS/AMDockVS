"""The Project action opens details for the active project from the top toolbar."""

import pytest

from amdockvs.runtime import AMDockVSRuntime
from amdockvs.ui.main_window import AMDockVSMainWindow
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_project_toolbar_opens_details_and_tracks_project_availability(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = AMDockVSMainWindow(runtime=runtime)
    errors = []
    opened = []
    try:
        window.show()
        app.processEvents()
        action = window.views.project_action
        assert not action.isEnabled()
        assert not hasattr(window._status_bar, "project_indicator")
        actions = window.views.catalog_toolbar.actions()
        assert actions[-3:] == [action, window.views.jobs_action, window.views.executors_action]
        assert not action.icon().isNull()

        runtime.create_project(name="Project details test", folder=tmp_path / "project")
        project_id = str(runtime.active_context.id)
        runtime.close_project()
        window._on_application_project_requested(project_id)
        assert action.isEnabled()
        assert "Project details test" in action.toolTip()

        def inspect_dialog():
            dialog = app.activeModalWidget()
            try:
                assert isinstance(dialog, QDialog)
                opened.append(dialog.windowTitle())
            except AssertionError as exc:
                errors.append(exc)
            finally:
                if dialog is not None:
                    dialog.accept()

        QTimer.singleShot(0, inspect_dialog)
        window.views.catalog_toolbar.widgetForAction(action).click()
        assert not errors
        assert opened == ["Project - Project details test"]
        runtime.close_project()
        window._sync_window_title()
        assert not action.isEnabled()
    finally:
        window.close()
        runtime.shutdown()
