"""The auxiliary zone keeps the size you give it, and preparation does not open it."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, DOCKING_VIEW_ID, PREP_STATUS_VIEW_ID


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = None
    try:
        runtime.create_project(name="aux_fit", folder=tmp_path / "project", description="")
        window = AMDockVSMainWindow(runtime=runtime)
        window.resize(1600, 900)
        window.show()
        app.processEvents()
        yield window
    finally:
        if window is not None:
            window.close()
        QThreadPool.globalInstance().waitForDone(10_000)
        runtime.shutdown()
        app.processEvents()


def _settle(predicate=lambda: False, timeout=2.0):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return predicate()


def test_docking_preparation_leaves_the_zone_closed(window):
    window.tools.open_tool(DOCKING_VIEW_ID)
    _settle()
    assert window.aux.occupant == PREP_STATUS_VIEW_ID  # one click away on the toggle
    assert not window.aux.action.isChecked()
    assert not window.central_widget.aux_widget.isVisible()
    window.aux.action.setChecked(True)  # what the user opened stays open when the tool closes
    window.tools.on_tool_action(DOCKING_VIEW_ID, False)
    _settle()
    assert window.aux.occupant == window.aux.AUX_DETAILS
    assert window.central_widget.aux_widget.isVisible()


def test_a_dragged_zone_keeps_its_size(window):
    central = window.central_widget
    splitter = central.content_splitter
    central.open_or_focus_view(COMPLEXES_VIEW_ID)
    window.aux.action.setChecked(True)
    window.aux.on_toggled(True)
    _settle()
    total = sum(splitter.sizes())
    splitter.setSizes([total * 4 // 10, total * 6 // 10])
    _settle()
    before = splitter.sizes()
    window.aux.on_toggled(True)
    _settle()
    assert splitter.sizes() == before
