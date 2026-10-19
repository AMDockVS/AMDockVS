"""The Help tour is informative and leaves project data and application views alone."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6's feature importer
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amdockvs.ui.main_window import AMDockVSMainWindow


@pytest.mark.parametrize("with_project", [False, True])
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_help_tour_with_and_without_project_is_read_only(tmp_path, monkeypatch, with_project):
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-tutorial-test"])
    runtime = AMDockVSRuntime()
    window = None
    try:
        if with_project:
            runtime.create_project(name="practice", folder=tmp_path / "practice", description="")
        window = AMDockVSMainWindow(runtime=runtime)
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        assert not hasattr(window, "_interface_tutorial")
        before_project = runtime.active_context
        before_projects = [project.id for project in runtime.list_projects()]
        before_tabs = dict(window.central_widget._open_tabs)
        before_page = window.central_widget.currentWidget()
        before_tools = dict(window.tools.widgets)

        def forbidden(*args, **kwargs):
            pytest.fail("An informative tour must not execute application actions")

        monkeypatch.setattr(runtime, "submit_job", forbidden)
        monkeypatch.setattr(runtime, "open_project", forbidden)
        monkeypatch.setattr(runtime, "create_project", forbidden)
        monkeypatch.setattr(window, "open_or_focus_view", forbidden)
        help_action = next(action for action in window.menuBar().actions() if action.text() == "&Help")
        help_menu = help_action.menu()
        tour_action = next(action for action in help_menu.actions() if action.text() == "Guided tour…")
        QTest.mouseClick(window.menuBar(), Qt.MouseButton.LeftButton,
                         pos=window.menuBar().actionGeometry(help_action).center())
        app.processEvents()
        QTest.mouseClick(help_menu, Qt.MouseButton.LeftButton,
                         pos=help_menu.actionGeometry(tour_action).center())
        app.processEvents()

        controller = window._interface_tutorial
        assert controller.is_active and controller.index == 0
        seen = []
        while controller.is_active:
            step = controller.current_step
            assert step.advance_on is None
            assert controller.overlay.isVisible()
            assert controller.overlay.next_button.isEnabled()
            if step.id == "appearance":
                assert step.title == "Interface theme"
                assert "Help" not in step.text
                assert controller.targets[step.target].widget.objectName() == "menuBarTheme"
            if step.id == "help":
                assert controller.targets[step.target].widget is window.menuBar()
                assert controller.overlay.hole.size() == window.menuBar().actionGeometry(help_action).size()
                assert "Help > Guided tour" in step.text
                if not with_project:
                    assert "project tour" in step.text and "File > Projects" in step.text
            seen.append(step.id)
            QTest.mouseClick(controller.overlay.next_button, Qt.MouseButton.LeftButton)
            app.processEvents()
        assert seen == (["projects", "catalog", "docking", "workspace", "status", "appearance", "help"]
                        if with_project else ["projects", "workspace", "appearance", "help"])
        assert runtime.active_context is before_project
        assert [project.id for project in runtime.list_projects()] == before_projects
        assert window.central_widget._open_tabs == before_tabs
        assert window.central_widget.currentWidget() is before_page
        assert window.tools.widgets == before_tools
        assert window._app_widget._projects_widget is None
        assert window.jobs_dialog is None

        tour_action.trigger()
        assert window._interface_tutorial is controller and controller.index == 0
        QTest.keyClick(controller.overlay.next_button, Qt.Key.Key_Escape)
        assert not controller.is_active
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_renamed_menu_does_not_break_the_tour(tmp_path, monkeypatch):
    from amdockvs.ui.tutorials import start_interface_tour

    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-tutorial-test"])
    runtime = AMDockVSRuntime()
    window = None
    try:
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()
        for action in window.menuBar().actions():
            action.setText(action.text().replace("&Help", "&Ayuda"))
        controller = start_interface_tour(window)
        assert controller.is_active and controller.targets["help"].widget is window.menuBar()
        controller.stop()
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
