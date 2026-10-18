import pytest

from amdockvs.runtime import AMDockVSRuntime

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.resources.icons import themed as themed_icons


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_project_switch_and_deleting_the_open_project_hot_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    runtime = AMDockVSRuntime()
    window = None
    real_close = None
    restarts: list[tuple] = []
    try:
        runtime.create_project(
            name="ui_active_project",
            folder=tmp_path / "ui_active_project",
            description="ui project switch test",
        )
        window = AMDockVSMainWindow(runtime=runtime)
        real_close = window.close
        window.show()
        app.processEvents()

        monkeypatch.setattr("amdockvs.app.restart", lambda _window, *args: restarts.append(args))
        monkeypatch.setattr(
            "amdockvs.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.Yes,
        )
        window._on_application_project_requested("next-project")
        window._app_widget.open_projects()
        browser = window._app_widget._projects_widget.project_browser
        assert browser.active_project_id == str(runtime.active_context.id)
        browser.active_project_delete_requested.emit(browser.active_project_id)

        assert restarts == [("--project-id", "next-project"),
                            ("--delete-project-id", str(runtime.active_context.id))]
        assert themed_icons._watcher is not None

        themed_icons.shutdown_themed_icons(app)

        assert themed_icons._watcher is None
        assert themed_icons._color_scheme_slot is None
        assert not themed_icons.ThemedIconEngine._pixmaps
        assert not themed_icons.ThemedIconEngine._renderers
    finally:
        if real_close is not None:
            real_close()
        themed_icons.shutdown_themed_icons(app)
        runtime.shutdown()
