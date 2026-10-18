"""Notification log: history survives the tab being closed, badge counts only what's unseen."""

import pytest

import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6 (see test_ui_job_feedback)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from PySide6.QtGui import QIcon  # noqa: E402

from amdockvs.ui.shell.notifications import (  # noqa: E402
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


def test_items_show_the_severity_svg_or_the_glyph_while_it_is_missing(app):
    from amdockvs.ui.shell.notifications import severity_icon

    log = NotificationLog([Notification("ok"), Notification("bad", level=WARNING)])
    info, warning = log._list.item(1), log._list.item(0)
    assert not info.icon().isNull() and not info.text().startswith("ℹ")  # info.svg ships
    if severity_icon(WARNING) is None:
        assert warning.icon().isNull() and warning.text().startswith("⚠")
    else:
        assert not warning.icon().isNull() and not warning.text().startswith("⚠")


def test_double_click_opens_the_whole_entry_with_its_traceback(app):
    note = Notification("Jobs failed", "boom", ERROR, details="Traceback (most recent call last): ...")
    log = NotificationLog([note])
    log._list.itemDoubleClicked.emit(log._list.item(0))
    assert log.entry_dialog.text() == "boom"
    assert log.entry_dialog.detailedText() == note.details
    log.entry_dialog.close()


def test_a_started_job_entry_opens_the_jobs_monitor_not_a_dialog(app):
    opened = []
    log = NotificationLog([Notification("Docking started.", open=lambda: opened.append(True))])
    log._list.itemDoubleClicked.emit(log._list.item(0))
    assert opened == [True] and not hasattr(log, "entry_dialog")


def test_storage_and_import_entries_open_where_the_problem_is(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import amdockvs.io.import_stats as import_stats
    from amdockvs.ui.shell import job_feedback
    from amdockvs.ui.view_ids import MOLECULES_VIEW_ID

    opened = []
    window = SimpleNamespace(
        runtime=SimpleNamespace(check_storage=lambda: (WARNING, "Storage leftovers", "3 orphans"),
                                get_project_resource_path=lambda _name: tmp_path),
        _show_project_summary=lambda: opened.append("summary"),
        open_or_focus_view=opened.append,
        statusBar=lambda: SimpleNamespace(showMessage=lambda *_a: None),
    )
    feedback = object.__new__(job_feedback.JobFeedbackController)  # its timers want a real window
    feedback.w, feedback.notifications = window, []
    feedback.notification_log = SimpleNamespace(append=lambda _n: None, isVisible=lambda: True)
    monkeypatch.setattr(job_feedback, "run_async", lambda fn, done, on_error=None: done(fn()))
    monkeypatch.setattr(import_stats, "drain_import_stats", lambda _path: {import_stats.IMPORTED: 4})

    feedback._check_storage()
    feedback._report_import_summary()
    for note in feedback.notifications:
        note.open()
    assert opened == ["summary", MOLECULES_VIEW_ID]
