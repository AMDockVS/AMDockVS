"""A background read belongs to the widget that asked for it, and outlives nothing."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import delete

from amdockvs.ui.common.async_query import _ACTIVE_TASKS, drain_async_reads, run_async


def _wait(app, condition, limit=10.0):
    deadline = time.monotonic() + limit
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


@pytest.fixture
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance
    drain_async_reads(10_000)
    instance.processEvents()


@pytest.mark.parametrize("fails", [False, True])
def test_a_read_whose_owner_is_gone_is_not_delivered(app, fails):
    """The query cannot be stopped mid-flight, so the answer is dropped instead."""
    started, delivered, errors = threading.Event(), [], []
    release = threading.Event()
    owner = QWidget()

    def work():
        started.set()
        release.wait(10)
        if fails:
            raise RuntimeError("read failed after the panel went away")
        return "receptor ids"

    run_async(work, delivered.append, errors.append, owner=owner)
    assert started.wait(10)
    delete(owner)  # the panel is closed while its read is still in the database
    release.set()

    _wait(app, lambda: not _ACTIVE_TASKS)
    assert delivered == [] and errors == []


@pytest.mark.parametrize("fails", [False, True])
def test_a_read_whose_owner_is_alive_is_delivered(app, fails):
    delivered, errors = [], []
    owner = QWidget()

    def work():
        if fails:
            raise RuntimeError("boom")
        return "receptor ids"

    run_async(work, delivered.append, errors.append, owner=owner)
    _wait(app, lambda: bool(delivered or errors))
    assert [str(error) for error in errors] == (["boom"] if fails else [])
    assert delivered == ([] if fails else ["receptor ids"])
    owner.deleteLater()


def test_an_unowned_read_is_delivered_as_before(app):
    delivered = []
    run_async(lambda: 7, delivered.append)
    _wait(app, lambda: bool(delivered))
    assert delivered == [7]


def test_draining_waits_for_a_read_to_leave_the_database(app):
    """What a caller does before closing the project the read is holding open."""
    finished = []

    def work():
        time.sleep(0.3)
        finished.append("left the database")
        return None

    run_async(work, lambda _result: None)
    assert drain_async_reads(10_000) is True
    assert finished == ["left the database"]  # the drain returned only once the read was out


def test_draining_gives_up_rather_than_hanging(app):
    """A read that never comes back must not keep the application from closing."""
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(30)
        return None

    run_async(work, lambda _result: None)
    assert started.wait(10)
    assert drain_async_reads(200) is False  # said so rather than waiting for ever
    release.set()
    assert drain_async_reads(10_000) is True


def test_the_preparation_panel_owns_the_reads_it_starts(monkeypatch):
    """Its results go into the studio's widgets, so a closed studio must not receive them."""
    from types import SimpleNamespace

    from amdockvs.ui.tools.docking import preparation_panel

    calls = []
    monkeypatch.setattr(preparation_panel, "run_async",
                        lambda fn, on_result, on_error=None, **kwargs: calls.append(kwargs))
    studio = SimpleNamespace(
        _catalog_receptor_widget=lambda: object(),
        _push_receptor_scope=lambda _widget: None,
        _method=lambda: "vina",
        runtime=SimpleNamespace(docking=SimpleNamespace(method_misfit_receptor_ids=lambda **_kw: []),
                                molecules=SimpleNamespace(excluded_since=lambda _since: [])),
        _prep_started={"receptor": 3},
    )
    panel = preparation_panel.PreparationPanel
    panel._sync_receptor_table_filter(studio)
    panel._report_prep_failures(studio, SimpleNamespace(noun="receptor"))

    assert len(calls) == 2
    assert [call.get("owner") for call in calls] == [studio, studio]
