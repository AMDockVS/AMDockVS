"""Docking Results as one view with pivots.

Hits (receptor > ligand > pose), off-target (ligand x receptor) and redocking (pose vs
reference) are the same results read three ways, so they are pivots of one tab instead of
three entries in the top toolbar.

A pivot is gated on being **populated**, never on whether a run happened: a project keeps
receiving molecules and runs, so the gate has to reopen by itself. And a pivot that cannot
be used is shown disabled *with the reason*, never hidden — hiding it makes the feature not
exist for whoever has not produced its data yet.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from amdockvs.ui.async_query import run_async

RESULTS_PIVOT_VIEW_ID = "workspace.complexes"  # keeps the tab identity of Docking Results

_HITS, _OFFTARGET, _REDOCKING = "hits", "offtarget", "redocking"
_AVAILABILITY_POLL_SECONDS = 10.0

# key -> (label, reason it is unavailable)
_PIVOTS = (
    (_HITS, "Hits (receptor › ligand › pose)", "No docking results yet"),
    (_OFFTARGET, "Off-target (ligand × receptor)", "Needs a ligand docked against 2 receptors"),
    (_REDOCKING, "Redocking (pose vs reference)", "No redocking runs yet"),
)


def _freshness_text_and_delay(age_seconds: float) -> tuple[str, int]:
    """Human freshness plus milliseconds until the next visible text boundary."""
    age = max(0.0, float(age_seconds))
    whole = int(age)
    if whole < 1:
        return "Updated just now", max(100, int((1.0 - age) * 1000))
    if whole < 10:
        next_boundary = whole + 1
        return f"Updated {whole}s ago", max(100, int((next_boundary - age) * 1000))
    if whole < 30:
        shown = (whole // 5) * 5
        return f"Updated {shown}s ago", max(100, int((shown + 5 - age) * 1000))
    if whole < 60:
        shown = (whole // 10) * 10
        return f"Updated {shown}s ago", max(100, int((shown + 10 - age) * 1000))
    minutes = whole // 60
    return f"Updated {minutes}m ago", max(100, int(((minutes + 1) * 60 - age) * 1000))


class ResultsPivotWidget(QWidget):
    def __init__(self, *, runtime, load_hit_in_pymol=None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._load_hit_in_pymol = load_hit_in_pymol
        self._pages: dict[str, QWidget] = {}
        self._available: dict[str, bool] = {}
        self._current_key = _HITS
        self._monitor_snapshot = None
        self._last_data_refresh: dict[str, float] = {}
        self._view_visible = False
        self._availability_loading = False
        self._availability_last_requested = 0.0

        outer = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("View:", self))
        self.pivot_combo = QComboBox(self)
        for key, label, _reason in _PIVOTS:
            self.pivot_combo.addItem(label, key)
        self.pivot_combo.currentIndexChanged.connect(self._on_pivot_changed)
        controls.addWidget(self.pivot_combo)
        self.reason_label = QLabel("", self)
        controls.addWidget(self.reason_label, 1)
        self.activity = QWidget(self)
        activity_layout = QHBoxLayout(self.activity)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        activity_layout.setSpacing(12)
        self.progress_label = QLabel("", self.activity)
        self.progress_label.setObjectName("resultsProgress")
        self.updated_label = QLabel("", self.activity)
        self.updated_label.setObjectName("resultsFreshness")
        activity_layout.addWidget(self.progress_label)
        activity_layout.addWidget(self.updated_label)
        controls.addWidget(self.activity)
        self.activity.hide()
        outer.addLayout(controls)

        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack, 1)
        self._show_pivot(_HITS)
        self._reload_availability(force=True)
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setSingleShot(True)
        self._freshness_timer.timeout.connect(self._render_freshness)
        QTimer.singleShot(0, self._bind_monitor)

    # --- pivots ---------------------------------------------------------------
    def _build_page(self, key: str) -> QWidget:
        # Imported here: each pivot is a live table, so it is built the first time it is
        # picked and never for a project that only ever looks at hits.
        if key == _OFFTARGET:
            from amdockvs.ui.tools.docking.offtarget import OffTargetResultsWidget as cls
        elif key == _REDOCKING:
            from amdockvs.ui.tools.docking.redocking import RedockingWidget as cls
        else:
            from amdockvs.ui.workspace import ComplexWidget as cls
        return cls(
            runtime=self.runtime,
            load_hit_in_pymol=self._load_hit_in_pymol,
            parent=self.stack,
        )

    def _show_pivot(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            page = self._pages[key] = self._build_page(key)
            self.stack.addWidget(page)
            refreshed = getattr(page, "data_refreshed", None)
            if refreshed is not None and hasattr(refreshed, "connect"):
                refreshed.connect(lambda changed=True, k=key: self._on_page_refreshed(k, changed))
        self.stack.setCurrentWidget(page)
        # Each pivot brings its own auxiliary panel (or none): tell the window to re-ask.
        sync = getattr(self.window(), "_set_aux_occupant", None)
        if callable(sync):
            sync()

    def aux_panel(self):
        """Delegated to the pivot on screen — only Hits has a "Selected Result" panel."""
        panel = getattr(self.current_page(), "aux_panel", None)
        return panel() if callable(panel) else None

    def _on_pivot_changed(self, _index: int) -> None:
        key = str(self.pivot_combo.currentData() or _HITS)
        reason = self._reason(key)
        self.reason_label.setText(reason)
        if reason:
            # A disabled item is out of reach for the mouse but not for the keyboard: say why
            # and stay where we were, rather than naming one pivot while showing another.
            self._select(self._current_key)
            return
        self._current_key = key
        self._show_pivot(key)
        if self._view_visible and self._active_jobs():
            self.refresh_counts()
        self._update_activity()

    def _select(self, key: str) -> None:
        index = self.pivot_combo.findData(key)
        if index >= 0 and index != self.pivot_combo.currentIndex():
            self.pivot_combo.blockSignals(True)
            self.pivot_combo.setCurrentIndex(index)
            self.pivot_combo.blockSignals(False)

    def _reason(self, key: str) -> str:
        if self._available.get(key, True):
            return ""
        return next(reason for k, _label, reason in _PIVOTS if k == key)

    # --- availability ---------------------------------------------------------
    def _reload_availability(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self._availability_loading:
            return
        if not force and now - self._availability_last_requested < _AVAILABILITY_POLL_SECONDS:
            return
        self._availability_loading = True
        self._availability_last_requested = now
        run_async(
            self.runtime.docking.pivot_availability,
            self._finish_availability,
            on_error=lambda _exc: self._finish_availability({}),
        )

    def _finish_availability(self, available: dict[str, bool]) -> None:
        self._availability_loading = False
        self._apply_availability(available)

    def _apply_availability(self, available: dict[str, bool]) -> None:
        self._available = dict(available or {})
        model = self.pivot_combo.model()
        for row, (key, label, reason) in enumerate(_PIVOTS):
            usable = self._available.get(key, True)
            item = model.item(row)
            item.setEnabled(usable)
            item.setToolTip("" if usable else reason)
            self.pivot_combo.setItemText(row, label if usable else f"{label} — {reason}")
        self.reason_label.setText(self._reason(str(self.pivot_combo.currentData() or _HITS)))

    # --- live job context -----------------------------------------------------
    def _bind_monitor(self) -> None:
        bridge = getattr(self.window(), "monitor_bridge", None)
        if bridge is None:
            return
        bridge.project_snapshot_updated.connect(self._on_monitor_snapshot)
        self._on_monitor_snapshot(getattr(bridge, "current_snapshot", None))

    def _on_monitor_snapshot(self, snapshot) -> None:
        self._monitor_snapshot = snapshot
        self._update_activity()

    @staticmethod
    def _job_kind(job) -> str:
        task_type = str(getattr(job, "task_type", "") or "").lower()
        if "redocking" in task_type:
            return _REDOCKING
        if "docking" in task_type:
            return _HITS
        return ""

    def _active_jobs(self) -> list:
        wanted = _REDOCKING if self._current_key == _REDOCKING else _HITS
        jobs = list(getattr(self._monitor_snapshot, "jobs", ()) or ())
        return [
            job for job in jobs
            if self._job_kind(job) == wanted
            and str(getattr(job, "status", "") or "").lower()
            not in {"completed", "failed", "canceled"}
        ]

    def _update_activity(self) -> None:
        if not self._view_visible:
            self.activity.hide()
            self._freshness_timer.stop()
            return
        jobs = self._active_jobs()
        if not jobs:
            self.activity.hide()
            self._freshness_timer.stop()
            return
        labels = {
            _HITS: "Hits",
            _OFFTARGET: "Off-target",
            _REDOCKING: "Redocking",
        }
        done = sum(max(0, int(getattr(job, "chunks_done", 0) or 0)) for job in jobs)
        total = sum(max(0, int(getattr(job, "chunks_total", 0) or 0)) for job in jobs)
        failed = sum(
            max(0, int(getattr(job, "chunks_failed", 0) or 0))
            + max(0, int(getattr(job, "chunks_stage_failed", 0) or 0))
            for job in jobs
        )
        progress = f"{done:,} / {total:,} dockings" if total else f"{done:,} dockings completed"
        if failed:
            progress += f" · {failed:,} failed"
        progress_text = f"{labels[self._current_key]} · {progress}"
        if self.progress_label.text() != progress_text:
            self.progress_label.setText(progress_text)
        self.activity.show()
        self._render_freshness()

    def _render_freshness(self) -> None:
        if not self._view_visible or self.activity.isHidden() or not self._active_jobs():
            self._freshness_timer.stop()
            return
        last_refresh = float(self._last_data_refresh.get(self._current_key, 0.0))
        if last_refresh <= 0:
            text, delay_ms = "Waiting for new results…", 1000
        else:
            text, delay_ms = _freshness_text_and_delay(time.monotonic() - last_refresh)
        if self.updated_label.text() != text:
            self.updated_label.setText(text)
        self._freshness_timer.start(delay_ms)

    def _on_page_refreshed(self, key: str, changed: bool = True) -> None:
        if not changed:
            return
        # Data time is model state, not view state. A query may finish after its page was
        # hidden; remember the change, but repaint only when that pivot is actually visible.
        self._last_data_refresh[key] = time.monotonic()
        if not self._view_visible or key != self._current_key:
            return
        self._freshness_timer.stop()
        self._render_freshness()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._view_visible = True
        if self._active_jobs():
            # Visibility only resumes rendering. The stored data timestamp is intentionally
            # preserved, so reopening the tab cannot masquerade as a result update.
            self._update_activity()
            self.refresh_counts()

    def hideEvent(self, event) -> None:
        self._view_visible = False
        self._freshness_timer.stop()
        self.activity.hide()
        super().hideEvent(event)

    def _refresh_current_page(self) -> None:
        page = self.stack.currentWidget()
        for name in ("refresh_results_view", "refresh_view", "refresh"):
            fn = getattr(page, name, None)
            if callable(fn):
                fn()
                return

    # --- what the window calls ------------------------------------------------
    def current_page(self) -> QWidget:
        """The pivot on screen — what a caller holding "the results view" actually wants."""
        return self.stack.currentWidget()

    def refresh(self) -> None:
        # Jobs finished: the gates may have just opened, and only the pivot on screen reloads.
        self._reload_availability(force=True)
        self._refresh_current_page()

    def refresh_counts(self) -> bool:
        """Live cadence: update gates and the active pivot's non-intrusive counters."""
        if not self._view_visible or not self._active_jobs():
            return True
        self._reload_availability()
        refresh_counts = getattr(self.current_page(), "refresh_counts", None)
        return bool(refresh_counts()) if callable(refresh_counts) else True

    def ensure_viewport_filled(self, force: bool = False) -> bool:
        """Delegate the one permitted live row refresh: fill the initial viewport once."""
        if not self._view_visible or not self._active_jobs():
            return True
        page = self.current_page()
        ensure = getattr(page, "ensure_viewport_filled", None)
        return bool(ensure(force)) if callable(ensure) else True

    refresh_view = refresh


__all__ = ["RESULTS_PIVOT_VIEW_ID", "ResultsPivotWidget"]
