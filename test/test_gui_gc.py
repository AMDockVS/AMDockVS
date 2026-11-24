"""Cyclic garbage is collected on the GUI thread only (amdockvs.ui.common.gui_gc)."""
from __future__ import annotations

import gc
import threading
import weakref

import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6

from amdockvs.ui.common import gui_gc


class _Cycle:
    def __init__(self):
        self.me = self


def test_install_turns_automatic_collection_off_and_starts_the_timer_once():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["gc-test"])
    gui_gc.install(app)
    collector = app._gui_thread_gc
    gui_gc.install(app)
    assert app._gui_thread_gc is collector
    assert not gc.isenabled()
    assert collector._timer.isActive() and collector._timer.interval() == gui_gc.INTERVAL_MS


def test_a_worker_allocating_never_collects_and_the_gui_thread_does(monkeypatch):
    gc.disable()
    monkeypatch.setattr(gc, "get_threshold", lambda: (10, 10, 10))
    dead = _Cycle()
    alive = weakref.ref(dead)
    del dead

    def allocate():  # far past every threshold: the automatic collector would have run here
        for _ in range(5000):
            _Cycle()

    worker = threading.Thread(target=allocate)
    worker.start()
    worker.join()
    assert alive() is not None, "collected on the worker"
    gui_gc.collect_due()
    assert alive() is None
