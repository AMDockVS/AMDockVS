"""Notification log: history survives the tab being closed, badge counts only what's unseen."""

import pytest

import amdockvs.configuration  # noqa: F401  - pydantic before PySide6 (see test_ui_job_feedback)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from PySide6.QtGui import QIcon  # noqa: E402

from amdockvs.ui.notifications import (  # noqa: E402
    ERROR,
    INFO,
    WARNING,
    Notification,
    NotificationBell,
    NotificationLog,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_line_carries_time_title_and_severity_icon():
    note = Notification("Import summary", "Imported 2 of 4", WARNING)
    line = note.as_line()
    assert "⚠" in line and "Import summary" in line and "Imported 2 of 4" in line
    assert f"{note.at:%H:%M}" in line
    assert Notification("Job started").as_line().startswith("ℹ")
    assert Notification("Boom", level=ERROR).as_line().startswith("⛔")


def test_log_rebuilds_newest_first_and_appends_on_top(app):
    entries = [Notification("first"), Notification("second")]
    log = NotificationLog(entries)
    assert [log._list.item(i).text().split("  ")[-1] for i in range(2)] == ["second", "first"]
    log.append(Notification("third"))
    assert log._list.item(0).text().endswith("third")
    assert log._list.count() == 3


def test_clear_empties_the_view_and_signals_the_owner(app):
    owner = [Notification("one")]
    log = NotificationLog(owner)
    log.cleared.connect(owner.clear)
    log._on_clear()
    assert log._list.count() == 0 and owner == []


def test_empty_log_shows_placeholder(app):
    log = NotificationLog([])
    assert log._empty.isVisibleTo(log)
    log.append(Notification("x", level=INFO))
    assert not log._empty.isVisibleTo(log)


def test_bell_shows_count_only_when_unread(app):
    bell = NotificationBell(QIcon())
    assert bell.text() == "" and "unread" not in bell.toolTip()
    bell.set_unread(3, level=ERROR)
    assert bell.text().strip() == "3" and "3 unread" in bell.toolTip()
    assert "#e05561" in bell.styleSheet()  # error tint
    bell.set_unread(0)
    assert bell.text() == "" and "#e05561" not in bell.styleSheet()
