from types import SimpleNamespace

import pytest

from amdockvs.runtime import AMDockVSRuntime

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.resources.icons import themed as themed_icons


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_project_switch_closes_current_window_after_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")

    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    runtime = AMDockVSRuntime()
    window = None
    real_close = None
    launched: list[str] = []
    closed: list[bool] = []
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

        process = SimpleNamespace(poll=lambda: None)
        monkeypatch.setattr(
            window._app_widget,
            "launch_project",
            lambda project_id: (launched.append(project_id), process)[1],
        )
        monkeypatch.setattr(
            "amdockvs.ui.main_window.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.Yes,
        )
        monkeypatch.setattr(window, "close", lambda: closed.append(True))

        window._on_application_project_requested("next-project")

        assert launched == ["next-project"]
        assert closed == [True]
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
