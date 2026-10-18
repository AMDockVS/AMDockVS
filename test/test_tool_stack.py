"""One wide tool must not hold every other tool at its width."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401  (before PySide6, see test_build_ui)
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QWidget

from amdockvs.ui.shell.tool_coordinator import _CurrentPageStack


class _Page(QWidget):
    def __init__(self, width: int):
        super().__init__()
        self._width = width

    def minimumSizeHint(self):
        return QSize(self._width, 100)


def test_the_tool_stack_takes_the_minimum_of_the_page_on_show():
    app = QApplication.instance() or QApplication([])
    stack, narrow, wide = _CurrentPageStack(), _Page(250), _Page(728)
    stack.addWidget(wide)
    stack.addWidget(narrow)

    stack.setCurrentWidget(narrow)
    assert stack.minimumSizeHint().width() == 250
    stack.setCurrentWidget(wide)
    assert stack.minimumSizeHint().width() == 728
    stack.deleteLater()
    app.processEvents()
