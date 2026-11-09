"""The auxiliary zone grows to fit its occupant instead of making it scroll."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, RECEPTOR_VIEW_ID


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


def _fits(window):
    page = window.central_widget.aux_widget.currentWidget()
    content = page.widget()  # Results lends a scroll area
    return content.height() <= page.viewport().height()


def test_results_detail_fits_after_a_smaller_occupant_set_the_split(window):
    central = window.central_widget
    splitter = central.content_splitter
    central.open_or_focus_view(RECEPTOR_VIEW_ID)
    window.aux.action.setChecked(True)
    window.aux.on_toggled(True)
    _settle()
    total = sum(splitter.sizes())
    splitter.setSizes([total - 100, 100])  # a short zone: dragged, or seeded in a small window
    _settle()

    results = central.open_or_focus_view(COMPLEXES_VIEW_ID).stack.currentWidget()
    assert _settle(lambda: window.aux.occupant == window.aux.AUX_VIEW_PANEL)
    # Picking a result shows its fields: the zone must follow, not scroll.
    for widget in (results.detail_fields, results.detail_more_btn):
        widget.setVisible(True)
    results.detail_placeholder.setVisible(False)
    # Offscreen fonts are small enough for the fields alone; "Show details" adds up to 160 px,
    # more than the zone's minimum, like the fields do with a real display's fonts.
    results.detail_more.setPlainText("\n".join(f"field {index}: value" for index in range(12)))
    results.detail_more_btn.setChecked(True)
    assert _settle(lambda: _fits(window)), (splitter.sizes(), central.aux_widget.currentWidget().widget().height())
    assert splitter.sizes()[1] <= sum(splitter.sizes()) // 2  # the tables keep at least half


def test_a_zone_dragged_larger_is_not_shrunk(window):
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
