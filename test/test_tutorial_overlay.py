"""The tutorial keeps the app's colors and still highlights its interactive target."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import amdockvs.runtime  # pydantic before PySide6's feature importer
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWidget
from ms_components.ms_tutorial import Tutorial, TutorialEvent, TutorialStep, TutorialTarget

from amdockvs.ui.common.tutorial import TutorialController


def test_overlay_preserves_background_and_outlines_the_clickable_target():
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.resize(900, 600)
    palette = host.palette()
    palette.setColor(QPalette.Window, QColor("#233445"))
    palette.setColor(QPalette.Highlight, QColor("#65bfff"))
    host.setPalette(palette)
    host.setAutoFillBackground(True)
    button = QPushButton("Import Receptors…", host)
    button.setGeometry(360, 260, 180, 40)
    host.show()
    app.processEvents()
    before = host.grab().toImage()
    controller = TutorialController(targets={"import": TutorialTarget(button)},
        events={"imported": TutorialEvent(button.clicked)}, parent=host)
    try:
        controller.start(Tutorial("example", "Example", (
            TutorialStep("import", "import", "Load receptor", "Click Import Receptors.", "imported"),)))
        app.processEvents()
        overlay = controller.overlay
        sample = QPoint(30, 550)
        assert not overlay.panel.geometry().contains(sample)
        assert not overlay.hole.contains(sample)
        after = host.grab().toImage()
        assert after.pixelColor(sample) == before.pixelColor(sample)
        ring = QPoint(overlay.hole.left() - 3, overlay.hole.center().y())
        assert after.pixelColor(ring) != before.pixelColor(ring)
        assert overlay.hole.size() == button.size()
        button.move(360, 100)
        QTest.qWait(120)  # the shared controller tracks target movement every 80 ms
        moved = host.grab().toImage()
        assert moved.pixelColor(ring) == before.pixelColor(ring)
        QTest.mouseClick(button, Qt.LeftButton)
        app.processEvents()
        assert not controller.is_active
    finally:
        controller.stop()
        host.close()
        app.processEvents()


def test_accepting_a_dialog_preserves_the_tutorial_but_cancelling_stops_it():
    app = QApplication.instance() or QApplication([])
    for accepted in (True, False):
        dialog = QDialog()
        dialog.resize(600, 400)
        button = QPushButton("Save", dialog)
        button.setGeometry(450, 340, 100, 30)
        dialog.show()
        app.processEvents()
        controller = TutorialController(targets={"save": TutorialTarget(button)},
            events={"saved": TutorialEvent(button.clicked)}, parent=dialog)
        controller.start(Tutorial("project", "Project", (
            TutorialStep("create", "save", "Create a project", "Click Save.", "saved"),)))
        if accepted:
            dialog.accept()
            assert controller.is_active
        else:
            dialog.close()
            assert not controller.is_active
        controller.stop()
        dialog.close()
        app.processEvents()
