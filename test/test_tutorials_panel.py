"""The tutorial sidebar's dismissal and startup preference are independent."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication

from amdockvs.runtime import AMDockVSRuntime
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.shell.tutorials_panel import TutorialsPanel


def test_panel_dismissal_preference_and_reopening(tmp_path, monkeypatch):
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()

    class TopLevelPanelObserver(QObject):
        def __init__(self):
            super().__init__()
            self.shown = []

        def eventFilter(self, widget, event):
            if isinstance(widget, TutorialsPanel) and event.type() == QEvent.Show and widget.isWindow():
                self.shown.append(widget)
            return False

    observer = TopLevelPanelObserver()
    app.installEventFilter(observer)

    def make_window():
        window = AMDockVSMainWindow(runtime=runtime)
        window.resize(900, 700)
        window.show()
        app.processEvents()
        return window

    try:
        window = make_window()
        assert not observer.shown
        assert window.tutorials_panel.isVisible()
        assert window.tutorials_panel_action.isChecked()
        window.tutorials_panel.close_button.click()
        assert not window.tutorials_panel.isVisible()
        assert not window.tutorials_panel_action.isChecked()
        window.close()
        app.processEvents()

        window = make_window()
        assert window.tutorials_panel.isVisible()  # close is only for this session
        window.tutorials_panel.hide_on_startup_checkbox.click()
        assert not window.tutorials_panel.isVisible()
        window.close()
        app.processEvents()

        window = make_window()
        assert not window.tutorials_panel.isVisible()
        window.tutorials_panel_action.trigger()
        assert window.tutorials_panel.isVisible()
        assert window.tutorials_panel.hide_on_startup_checkbox.isChecked()
        window.tutorials_panel.hide_on_startup_checkbox.click()
        assert window.tutorials_panel.isVisible()
        window.close()
        app.processEvents()

        window = make_window()
        assert window.tutorials_panel.isVisible()
        assert not window.tutorials_panel.hide_on_startup_checkbox.isChecked()
        window.close()
        app.processEvents()
    finally:
        app.removeEventFilter(observer)
        runtime.shutdown()
