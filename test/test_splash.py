import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from amdockvs.ui.shell import splash as splash_module


def test_splash_show_does_not_spend_qt_mapping_timeout(monkeypatch):
    app = QApplication.instance() or QApplication(["amdockvs-splash-test"])
    monkeypatch.setattr(splash_module, "_build_pixmap", lambda: QPixmap(32, 32))

    started = time.monotonic()
    splash = splash_module.create_splash()
    elapsed = time.monotonic() - started

    splash.close()
    app.processEvents()
    assert elapsed < 0.5
