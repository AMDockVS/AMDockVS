import faulthandler
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Before PySide6: its import hook breaks pydantic's lazy shim while ms_flow is being imported.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401, E402
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


def test_the_gui_lag_goes_to_its_log(tmp_path, monkeypatch):
    import json

    from amdockvs.app import freeze_log, install_gui_lag_log

    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    timer = install_gui_lag_log(app)
    try:
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()
        stuck()
        end = time.monotonic() + 0.1
        while time.monotonic() < end:
            app.processEvents()
        app.aboutToQuit.emit()
        stall, minute = map(json.loads, freeze_log().with_name("gui_lag.jsonl").read_text().splitlines())
        assert minute["max_ms"] >= 250
        assert stall["stall_ms"] >= 250 and "gc_ms" in stall
    finally:
        timer.stop()


def stuck() -> None:
    time.sleep(0.3)  # a 300 ms hitch



def test_a_stall_says_how_much_of_it_was_the_gc(tmp_path, monkeypatch):
    import gc
    import json

    from amdockvs.app import freeze_log, install_gui_lag_log

    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    garbage = [[i] for i in range(3_000_000)]  # objects enough for a full collection to take a while
    timer = install_gui_lag_log(app)
    try:
        app.processEvents()
        gc.collect()
        app.processEvents()
        (stall,) = [json.loads(line) for line in freeze_log().with_name("gui_lag.jsonl").read_text().splitlines()]
        assert stall["gc_ms"] >= 50 and stall["gc_ms"] <= stall["stall_ms"] + 20
    finally:
        del garbage
        timer.stop()
