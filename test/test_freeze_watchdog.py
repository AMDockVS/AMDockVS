import faulthandler
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


def test_a_stalled_gui_thread_gets_its_stacks_dumped(tmp_path, monkeypatch):
    from amdockvs.app import freeze_log, install_freeze_watchdog

    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_FREEZE_SECONDS", "0.5")
    app = QApplication.instance() or QApplication([])
    timer = install_freeze_watchdog(app)
    try:
        app.processEvents()
        time.sleep(1.0)  # the event loop is stuck: nothing re-arms the timeout
        app.processEvents()
        assert "Timeout (0:00:00.500000)!" in freeze_log().read_text()
        assert "test_a_stalled_gui_thread_gets_its_stacks_dumped" in freeze_log().read_text()
    finally:
        timer.stop()
        faulthandler.cancel_dump_traceback_later()
