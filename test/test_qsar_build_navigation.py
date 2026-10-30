"""Panel appearance changes must preserve QSAR's existing navigation."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from amdockvs.ui.tools.qsar.panels import QSARBuildWidget


def test_qsar_preserves_back_continue_and_train(monkeypatch):
    app = QApplication.instance() or QApplication(["qsar-navigation-test"])
    trained = []
    monkeypatch.setattr(QSARBuildWidget, "refresh", lambda self: None)
    monkeypatch.setattr(QSARBuildWidget, "_train", lambda self: trained.append(True))
    widget = QSARBuildWidget(runtime=SimpleNamespace())
    widget.resize(650, 700)
    widget.show()
    app.processEvents()
    try:
        assert widget.stepper.btn_next.isVisible()
        assert widget.stepper.btn_back.isVisible()
        assert not widget.stepper.btn_back.isEnabled()
        for expected in (1, 2, 3):
            assert widget.stepper.btn_next.text() == "Continue"
            widget.stepper.btn_next.click()
            assert widget.stepper.current_index == expected
        assert not trained
        assert widget.stepper.btn_next.text() == "Train"
        widget.stepper.btn_back.click()
        assert widget.stepper.current_index == 2
        assert widget.stepper.btn_next.text() == "Continue"
        widget.stepper.btn_next.click()
        widget.stepper.btn_next.click()
        assert trained == [True]
    finally:
        widget.close()


def test_qsar_numbered_headers_navigate_without_starting_training(monkeypatch):
    app = QApplication.instance() or QApplication(["qsar-header-test"])
    trained = []
    monkeypatch.setattr(QSARBuildWidget, "refresh", lambda self: None)
    monkeypatch.setattr(QSARBuildWidget, "_train", lambda self: trained.append(True))
    widget = QSARBuildWidget(runtime=SimpleNamespace())
    widget.resize(650, 700)
    widget.show()
    app.processEvents()
    try:
        for index in (3, 1, 2, 0, 3):
            QTest.mouseClick(widget.stepper._header_items[index], Qt.LeftButton)
            assert widget.stepper.current_index == index
            assert not trained
        assert widget.stepper.btn_next.text() == "Train"
        widget.stepper.btn_next.click()
        assert trained == [True]
    finally:
        widget.close()
