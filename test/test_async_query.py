import threading

import pytest

# Load pydantic-backed project models before shiboken installs its feature importer.
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from amdockvs.ui.async_query import run_async


def test_run_async_keeps_task_alive_and_returns_on_gui_thread():
    QApplication.instance() or QApplication(["amdockvs-async-test"])
    gui_thread = threading.get_ident()
    loop = QEventLoop()
    received = []

    run_async(
        lambda: (threading.get_ident(), 42),
        lambda value: (received.append((threading.get_ident(), value)), loop.quit()),
    )
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert received
    callback_thread, (worker_thread, value) = received[0]
    assert value == 42
    assert worker_thread != gui_thread
    assert callback_thread == gui_thread
