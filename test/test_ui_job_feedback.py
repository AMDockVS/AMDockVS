"""Feedback while a job runs: the submit notification + the "fill the viewport once, then only counters"
refresh policy. Lives in its own module because test_amdock_ui.py is currently unimportable
(stale symbol), and it imports amdockvs (pydantic) before PySide6 so shiboken's feature loader
doesn't trip pydantic's lazy migration shim.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.runtime import AMDockVSRuntime

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import LIGANDS_VIEW_ID


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_dialog_on_submit_and_single_row_load_per_job_run(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_amdock_runtime import _make_smiles_file

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_refresh", folder=tmp_path / "ui_refresh", description="refresh policy")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        ligands_file = tmp_path / "toast.smi"
        _make_smiles_file(ligands_file, count=3)
        job_ids = runtime.loader.load_ligands([ligands_file], batch_size=2, executor_name="thread")
        app.processEvents()
        assert job_ids
        # Submit posts to the notification log and raises the menu-bar bell count.
        assert "started" in window.jobs.notifications[-1].title.lower()
        assert window.jobs.notification_bell.text().strip() == "1"
        runtime.wait_for_jobs(job_ids, timeout_s=120)

        view = window.central_widget.open_or_focus_view(LIGANDS_VIEW_ID)
        app.processEvents()
        calls = {"rows": 0, "counts": 0}
        monkeypatch.setattr(
            view, "ensure_viewport_filled",
            lambda force=False: (calls.__setitem__("rows", calls["rows"] + 1), True)[1],
        )
        monkeypatch.setattr(view, "refresh_counts", lambda: (calls.__setitem__("counts", calls["counts"] + 1), True)[1])

        window.jobs.on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=1, jobs=[]))
        assert window.jobs.view_refresh_timer.isActive()
        window.pymol_dock = SimpleNamespace(scene_memory=SimpleNamespace(interacting=True))
        window.jobs.refresh_current_view_in_background()
        assert calls == {"rows": 0, "counts": 0}
        window.pymol_dock = None
        for _ in range(4):
            window.jobs.refresh_current_view_in_background()
        # Rows loaded once; every later tick is counters-only.
        assert calls == {"rows": 1, "counts": 3}

        # Job run ends: one final count, timer stopped, and the next run may load rows again.
        window.jobs.on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=0, jobs=[]))
        assert not window.jobs.view_refresh_timer.isActive()
        assert calls["counts"] == 4

        # A new run starts from scratch, so the view loads rows once again.
        window.jobs.on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=2, jobs=[]))
        assert window.jobs.rows_loaded_views == set()
        window.jobs.refresh_current_view_in_background()
        assert calls["rows"] == 2
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


def test_a_failed_job_shows_its_error_and_keeps_the_traceback_for_the_details():
    from amdockvs.ui.shell.job_feedback import JobFeedbackController

    error = (  # as the process pool gives it: the traceback of the worker, then the one of the parent
        'loky.process_executor._RemoteTraceback: \n"""\nTraceback (most recent call last):\n'
        '  File "process_executor.py", line 490, in _process_worker\n    r = call_item()\n'
        'RuntimeError: QuickVina 2 is not installed.\n"""\n\n'
        "The above exception was the direct cause of the following exception:\n\n"
        'Traceback (most recent call last):\n  File "executors.py", line 359, in _call\n'
        "RuntimeError: QuickVina 2 is not installed. Install it from Settings > External tools."
    )

    def failure(job_error):
        detail = SimpleNamespace(job=SimpleNamespace(error=job_error), failures=[])
        bridge = SimpleNamespace(get_job_detail=lambda job_id, failure_limit=1: detail)
        controller = SimpleNamespace(w=SimpleNamespace(monitor_bridge=bridge),
                                     _summarize_failure_message=JobFeedbackController._summarize_failure_message)
        return JobFeedbackController._job_failure(controller, "job")

    assert failure(error) == (
        "RuntimeError: QuickVina 2 is not installed. Install it from Settings > External tools.", error)
    assert failure("job 3 failed") == ("job 3 failed", "")  # nothing more to say: no details to open


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_opening_a_project_checks_its_storage_off_the_gui_thread_and_reports_to_the_log(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_storage", folder=tmp_path / "ui_storage", description="storage check")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()
        seen = []
        healthy = runtime.check_storage()
        assert healthy[0] == "info"  # a healthy check stays out of the log; a problem gets in
        leftovers = ("warning", "Project storage has leftovers", "2 unused entries can be cleared by a repair.")
        monkeypatch.setattr(runtime, "check_storage", lambda: (seen.append(threading.current_thread()), leftovers)[1])
        window.jobs.notifications.clear()
        window.jobs.on_monitor_project_changed(SimpleNamespace(project_id="another-project"))
        deadline = time.monotonic() + 10
        while not window.jobs.notifications and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert seen and seen[0] is not threading.main_thread()
        assert window.jobs.notifications[-1].title == "Project storage has leftovers"
        assert runtime.compact_storage_if_due() is None  # a new project has nothing dead
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()


def test_an_unhandled_gui_exception_lands_in_the_log_with_its_traceback():
    import sys

    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.shell.job_feedback import JobFeedbackController
    from amdockvs.ui.shell.notifications import ERROR

    QApplication.instance() or QApplication([])
    posted = []
    fake = type("Fake", (), {"post_notification": lambda self, *a, **k: posted.append((a, k))})()
    saved = sys.excepthook
    try:
        JobFeedbackController._catch_unhandled_exceptions(fake)
        try:
            raise ValueError("bad cell")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = saved
    (title, text, level), kwargs = posted[0]
    assert title == "Unexpected error: ValueError" and text == "bad cell" and level == ERROR
    assert "raise ValueError" in kwargs["details"]
