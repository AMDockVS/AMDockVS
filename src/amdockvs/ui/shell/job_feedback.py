"""Everything the window says about jobs: notifications, grouped failures, status-bar
indicators and the incremental refresh of the open views while a run is inserting rows."""

from __future__ import annotations

import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from amdockvs.ui.monitor import MONITOR_JOBS_VIEW_ID
from amdockvs.ui.notifications import (
    ERROR,
    INFO,
    MAX_ENTRIES,
    WARNING,
    Notification,
    NotificationBell,
    NotificationLog,
)
from amdockvs.ui.resources.icons import icon as load_icon


class JobFeedbackController:
    # While a job is inserting rows: poll this often for the first fill, and stop waiting for
    # a full viewport after this long (show whatever exists).
    FIRST_FILL_TICK_MS = 250
    FIRST_FILL_MAX_WAIT_S = 3.0

    def __init__(self, window):
        self.w = window
        self.notifications: list[Notification] = []
        self.notification_bell = None
        self.notification_log = None
        self._unread_notifications = 0
        self._unread_level = INFO
        self.jobs_active = False
        self.jobs_status = (0, 0.0)
        # View ids whose rows were already loaded during the current job run. Rows load ONCE
        # per view per run, as early as possible; after that only the record counters keep
        # ticking and the user pulls new rows by scrolling.
        self.rows_loaded_views: set[str] = set()
        self._run_started_at = 0.0
        self._shown_failure_jobs: set[str] = set()
        self._persisted_seen_failure_jobs: set[str] = set()
        self._pending_failure_jobs: list[str] = []
        self._monitor_project_id: str = ""
        self.view_refresh_timer = QTimer(window)
        self.view_refresh_timer.setSingleShot(True)
        self.view_refresh_timer.setInterval(4000)
        self.view_refresh_timer.timeout.connect(self.refresh_current_view_in_background)
        self.failure_summary_timer = QTimer(window)
        self.failure_summary_timer.setSingleShot(True)
        self.failure_summary_timer.setInterval(300)
        self.failure_summary_timer.timeout.connect(self._flush_failure_summary)

    def stop_timers(self) -> None:
        self.view_refresh_timer.stop()
        self.failure_summary_timer.stop()

    # -- notifications -------------------------------------------------------------

    def build_bell(self):
        """The bell lives in the menu bar's corner; its drop-down IS the notification log."""
        self.notification_bell = NotificationBell(load_icon("bell.svg"), self.w)
        self.notification_bell.clicked.connect(self.open_notifications)
        self.notification_log = NotificationLog(self.notifications, parent=self.w)
        self.notification_log.setWindowFlags(Qt.Popup)  # closes on click-away
        self.notification_log.cleared.connect(self.notifications.clear)
        return self.notification_bell

    def post_notification(self, title: str, text: str = "", level: str = INFO) -> None:
        """Record something worth remembering. Unread count clears when the log is opened."""
        note = Notification(title=str(title), text=str(text), level=str(level))
        self.notifications.append(note)
        del self.notifications[:-MAX_ENTRIES]
        self.notification_log.append(note)
        if self.notification_log.isVisible():
            return  # dropped down right now — the user is looking straight at it
        self._unread_notifications += 1
        if level == ERROR or (level == WARNING and self._unread_level != ERROR):
            self._unread_level = level
        self.notification_bell.set_unread(self._unread_notifications, level=self._unread_level)

    def open_notifications(self) -> None:
        self.notification_log.popup_at(self.notification_bell)
        self._unread_notifications = 0
        self._unread_level = INFO
        self.notification_bell.set_unread(0)

    # -- job state -----------------------------------------------------------------

    def on_project_snapshot_updated(self, snapshot) -> None:
        if snapshot is None or not getattr(snapshot, "has_project", False):
            self.jobs_status = (0, 0.0)
            self.update_jobs_statusbar(snapshot)
            return
        was_active = self.jobs_active
        self.jobs_active = bool(int(getattr(snapshot, "jobs_active", 0) or 0) > 0)
        if self.jobs_active and not was_active:
            # New run: fill the viewport as soon as there are rows, then counters-only.
            self.rows_loaded_views.clear()
            self._run_started_at = time.monotonic()
            self.view_refresh_timer.start(self.FIRST_FILL_TICK_MS)
        if not self.jobs_active and was_active:
            self.view_refresh_timer.stop()
            self._refresh_counts_only()  # final total once the run is over
        # Global progress = mean progress over the non-terminal (active) jobs.
        active_jobs = [job for job in getattr(snapshot, "jobs", []) if not job.is_terminal]
        progress = (
            sum(float(job.progress or 0.0) for job in active_jobs) / len(active_jobs)
            if active_jobs
            else 0.0
        )
        self.jobs_status = (len(active_jobs), progress)
        self.update_jobs_statusbar(snapshot)

    def on_job_submitted_notice(self, job_name: str, _job_id: str) -> None:
        label = re.sub(r"^amdockvs[_ ]|[_ ]job$", "", str(job_name)).replace("_", " ").strip() or "Job"
        self.post_notification(
            f"{label.capitalize()} started.", "Running in the background — follow it in the Jobs monitor."
        )
        # Show its progress bar now instead of waiting for the monitor's idle poll.
        self.w.monitor_bridge.request_refresh()

    def _jobs_surfaces_open(self) -> bool:
        # A jobs surface is "on screen" only when the summary dock is visible or the
        # jobs view is the CURRENT tab — a jobs view sitting in a background tab does
        # not count, otherwise the status bar indicator would stay hidden with nothing
        # else showing. There must always be a monitor indicator visible.
        dock_visible = self.w.monitor_dock is not None and self.w.monitor_dock.isVisible()
        jobs_is_current = str(self.w.central_widget.current_view_id()) == MONITOR_JOBS_VIEW_ID
        return bool(dock_visible or jobs_is_current)

    def update_jobs_statusbar(self, snapshot=None) -> None:
        active, progress = self.jobs_status
        status_bar = self.w._status_bar
        status_bar.jobs_indicator.set_state(
            active=active,
            progress=progress,
            visible=not self._jobs_surfaces_open(),
        )
        try:
            runtime = self.w.runtime
            workflow = runtime.workflow if runtime.active_context is not None else None
            status_bar.workflow_indicator.set_state(
                steps=len(workflow.steps) if workflow else 0,
                status=workflow.status if workflow else "idle",
            )
        except Exception:
            status_bar.workflow_indicator.set_state(steps=0, status="idle")

        if snapshot is None:
            snapshot = getattr(self.w.monitor_bridge, "current_snapshot", None)
        self.update_resources_statusbar(snapshot)

    def update_resources_statusbar(self, snapshot=None) -> None:
        """CPU + backend already collected by the monitor thread; only paint them here."""
        status_bar = self.w._status_bar
        resources = getattr(snapshot, "resources", None)
        if resources is not None:
            status_bar.resource_indicator.set_state(
                used_cpu=int(getattr(resources, "cpu_used", 0) or 0),
                total_cpu=int(getattr(resources, "cpu_total", 0) or 0),
                used_gpu=int(getattr(resources, "gpu_used", 0) or 0),
                total_gpu=int(getattr(resources, "gpu_total", 0) or 0),
            )
        else:
            status_bar.resource_indicator.setVisible(False)
        compute = next(
            (
                executor for executor in list(getattr(snapshot, "executors", ()) or ())
                if str(getattr(executor, "name", "") or "") == "compute"
            ),
            None,
        )
        status_bar.backend_indicator.set_backend(getattr(compute, "backend", None))

    # -- failures ------------------------------------------------------------------

    @staticmethod
    def _failure_settings_key(project_id: str) -> str:
        return f"jobs/failed_seen/{project_id}"

    def on_monitor_project_changed(self, snapshot) -> None:
        project_id = str(getattr(snapshot, "project_id", "") or "")
        if not project_id or project_id == self._monitor_project_id:
            return
        self._monitor_project_id = project_id
        self._shown_failure_jobs.clear()
        self._pending_failure_jobs.clear()
        self.failure_summary_timer.stop()
        stored = self.w._settings.value(self._failure_settings_key(project_id), [])
        if isinstance(stored, str):
            stored = [stored] if stored.strip() else []
        self._persisted_seen_failure_jobs = {
            str(item).strip()
            for item in list(stored or [])
            if str(item).strip()
        }

    @staticmethod
    def _summarize_failure_message(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return "The job finished with an error."
        missing_3d_match = re.search(r"Missing has_3d for (\d+) ligand\(s\)", text)
        if missing_3d_match:
            count = int(missing_3d_match.group(1))
            return (
                f"{count} ligand(s) do not have 3D conformers.\n"
                "Generate 3D coordinates before running ligand preparation."
            )
        docking_missing_match = re.search(r"missing_counts=({.*})", text)
        if docking_missing_match:
            return (
                "Docking requirements are not satisfied.\n"
                f"{docking_missing_match.group(1)}"
            )
        if len(text) > 800:
            return text[:800].rstrip() + "..."
        return text

    def _job_failure_message(self, job_id: str) -> str:
        try:
            detail = self.w.monitor_bridge.get_job_detail(job_id, event_limit=20, output_limit=0, log_limit=0)
        except Exception:
            detail = None
        if detail is not None:
            failure_events = [
                event for event in detail.events
                if str(event.level or "").upper() in {"ERROR", "CRITICAL"}
                   or "fail" in str(event.event_type or "").lower()
            ]
            if failure_events:
                return self._summarize_failure_message(str(failure_events[-1].message or ""))
        job = self.w.runtime.get_job_status(job_id)
        if job is not None and str(job.last_scheduler_reason or "").strip():
            return self._summarize_failure_message(str(job.last_scheduler_reason or ""))
        return "The job finished with an error. Open the Jobs monitor for details."

    def _flush_failure_summary(self) -> None:
        pending = [job_id for job_id in self._pending_failure_jobs if job_id not in self._shown_failure_jobs]
        self._pending_failure_jobs.clear()
        if not pending:
            return
        lines: list[str] = []
        for index, job_id in enumerate(pending[:5], start=1):
            lines.append(f"{index}. {self._job_failure_message(job_id)}")
        hidden_count = max(0, len(pending) - 5)
        if hidden_count:
            lines.append(f"... and {hidden_count} more failed job(s). Open Jobs for details.")
        message = "\n\n".join(lines) if lines else "One or more jobs failed. Open Jobs for details."
        for job_id in pending:
            self._shown_failure_jobs.add(job_id)
            self._persisted_seen_failure_jobs.add(job_id)
        if self._monitor_project_id:
            self.w._settings.setValue(
                self._failure_settings_key(self._monitor_project_id),
                sorted(self._persisted_seen_failure_jobs),
            )
        # Modal on purpose: "your 8h run failed" cannot wait in a list the user may never open.
        self.post_notification("Jobs failed", message.replace("\n\n", " · "), ERROR)
        QMessageBox.warning(self.w, "Jobs Failed", message)

    def on_job_finished(self, job_id: str, status: str) -> None:
        self.w.views.refresh_open_views_once()
        self.w.diagram_dock.reload()  # a diagram job may have just cached the selected pose's model
        self._report_import_summary()
        normalized_job_id = str(job_id or "").strip()
        if (
                str(status or "").strip().lower() != "failed"
                or not normalized_job_id
                or normalized_job_id in self._shown_failure_jobs
                or normalized_job_id in self._persisted_seen_failure_jobs
        ):
            return
        self.w._status_bar.jobs_indicator.set_attention(True)  # red cue while monitor is hidden
        if normalized_job_id not in self._pending_failure_jobs:
            self._pending_failure_jobs.append(normalized_job_id)
        if not self.failure_summary_timer.isActive():
            self.failure_summary_timer.start()

    def _report_import_summary(self) -> None:
        """Surface per-molecule import accounting the workers drop as filesystem tallies (loky
        can't log to the monitor). Type-agnostic: non-import jobs write no tally, so draining is
        a no-op for them. Goes to the notification log so the breakdown is still there minutes
        later — a transient popup is exactly what made a filtered-out import undiagnosable."""
        from amdockvs.constants import RESOURCE_MOLECULES
        from amdockvs.io.import_stats import IMPORTED, drain_import_stats, summarize

        try:
            storage = self.w.runtime.get_project_resource_path(RESOURCE_MOLECULES)
        except Exception:  # noqa: BLE001 - no active project / resource: nothing to report
            return
        tally = drain_import_stats(storage)
        if not tally:
            return
        message = summarize(tally)
        self.w.statusBar().showMessage(message, 8000)
        skipped = sum(int(v or 0) for k, v in tally.items() if k != IMPORTED)
        self.post_notification("Import summary", message, WARNING if skipped else INFO)

    # -- incremental view refresh while a run inserts rows -------------------------

    def _refresh_counts_only(self) -> bool:
        """Keep the record counters live without reloading rows (see rows_loaded_views).
        False means "reload the rows instead" (empty table: nothing to preserve). A view with
        no refresh_counts simply stays quiet — it has a manual refresh button."""
        _view_id, widget = self.w.views.current_view()
        refresh_counts = getattr(widget, "refresh_counts", None)
        if not callable(refresh_counts):
            return True
        try:
            return bool(refresh_counts())
        except Exception:
            return False

    def refresh_current_view_in_background(self) -> None:
        if not self.jobs_active:
            return
        # PyMOL and these table reads share the GUI thread. A COUNT landing in the middle
        # of a drag is visible as a skipped frame, so defer it until the button is released.
        pymol = getattr(self.w, "pymol_dock", None)
        if bool(getattr(pymol, "_amdock_interacting", False)):
            self.view_refresh_timer.start(self.FIRST_FILL_TICK_MS)
            return
        view_id, widget = self.w.views.current_view()
        if widget is None:
            self.view_refresh_timer.start(self.FIRST_FILL_TICK_MS)
            return
        if view_id in self.rows_loaded_views:
            # Rows are the user's now: only the totals tick, scrolling pulls new pages.
            # An empty table has nothing to preserve, so let it load rows again.
            if self._refresh_counts_only():
                self.view_refresh_timer.start(3000)
                return
            self.rows_loaded_views.discard(view_id)
        # Hybrid trigger: poll cheaply and load rows as soon as there are enough to fill the
        # viewport — or after FIRST_FILL_MAX_WAIT_S, whichever comes first.
        force = (time.monotonic() - self._run_started_at) >= self.FIRST_FILL_MAX_WAIT_S
        filled = False
        ensure_filled = getattr(widget, "ensure_viewport_filled", None)
        if callable(ensure_filled):
            try:
                filled = bool(ensure_filled(force))
            except Exception:
                filled = False
        elif force:
            filled = self._refresh_view_rows(widget)
        if filled:
            self.rows_loaded_views.add(view_id)
        if self.jobs_active:
            self.view_refresh_timer.start(3000 if filled else self.FIRST_FILL_TICK_MS)

    @staticmethod
    def _refresh_view_rows(widget) -> bool:
        """Full reload for views without the viewport-fill API (docking results, activity…)."""
        background_refresh = getattr(widget, "background_refresh", None)
        if callable(background_refresh):
            try:
                if bool(background_refresh()):
                    return True
            except Exception:
                pass
        refresh = getattr(widget, "refresh", None)
        if callable(refresh) and not getattr(widget, "hasFocus", lambda: False)():
            try:
                refresh()
                return True
            except Exception:
                return False
        return False
