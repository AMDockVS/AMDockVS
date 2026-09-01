from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from amdockvs.ui.resources.icons import icon as load_icon
from ms_components.ms_dockwidget.widget import DockManager, MSDockWidget
from ms_components.ms_monitor import MolSuiteMonitorBridge, MolSuiteMonitorWidget
from ms_components.ms_monitor.job_cards import GlowBar, status_color


class JobBar(QWidget):
    """One compact row for the dock: name · glow bar · percent. Bars only, no chrome."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(8)
        self._name = QLabel("", self)
        self._name.setMinimumWidth(110)
        self._name.setMaximumWidth(180)
        self._name.setStyleSheet("font-size:11px;")
        self._bar = GlowBar(0.0, "running")
        self._bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._pct = QLabel("0%", self)
        self._pct.setFixedWidth(42)
        self._pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._pct.setStyleSheet("font-size:11px;")
        row.addWidget(self._name)
        row.addWidget(self._bar, 1)
        row.addWidget(self._pct)

    def set_job(self, job) -> None:
        name = job.task_type or job.origin_id or job.job_id[:8]
        self._name.setText(name)
        self._name.setToolTip(f"{name}\n{job.job_id}\n{job.status}")
        color = status_color(job.status)
        self._bar.set_value(float(job.progress or 0.0), job.status)
        self._pct.setText(f"{float(job.progress or 0.0):.0f}%")
        self._pct.setStyleSheet(f"font-size:11px; color:{color};")


class MonitorSummaryWidget(QWidget):
    open_requested = Signal()

    def __init__(self, *, bridge: MolSuiteMonitorBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._snapshot = None

        self.MAX_BARS = 8
        self._bars: list[JobBar] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel("No active project", self)
        self._title.setStyleSheet("font-weight:600;")
        self._jobs_label = QLabel("0 jobs", self)
        self._jobs_label.setStyleSheet("color:palette(placeholder-text); font-size:11px;")
        header.addWidget(self._title, 1)
        header.addWidget(self._jobs_label)
        layout.addLayout(header)

        # Bars container — the whole point of the dock.
        self._bars_box = QVBoxLayout()
        self._bars_box.setSpacing(2)
        layout.addLayout(self._bars_box)
        self._empty_label = QLabel("No jobs", self)
        self._empty_label.setStyleSheet("color:palette(placeholder-text); font-size:11px;")
        self._bars_box.addWidget(self._empty_label)

        layout.addStretch(1)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self._refresh_button = QPushButton("Refresh", self)
        self._open_button = QPushButton("Open Jobs", self)
        actions_row.addWidget(self._refresh_button)
        actions_row.addWidget(self._open_button)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        self._refresh_button.clicked.connect(self._bridge.request_refresh)
        self._open_button.clicked.connect(self.open_requested.emit)
        self._bridge.project_snapshot_updated.connect(self.update_snapshot)
        self._bridge.project_cleared.connect(self.clear_snapshot)

    def clear_snapshot(self) -> None:
        self._snapshot = None
        self._title.setText("No active project")
        self._jobs_label.setText("0 jobs")
        self._render_jobs([])

    def update_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        if snapshot is None or not snapshot.has_project:
            self.clear_snapshot()
            return
        self._title.setText(snapshot.project_name or snapshot.project_id or "Active project")
        active = int(snapshot.jobs_active or 0)
        self._jobs_label.setText(f"{active} active")
        # Running jobs first, then the rest, so the dock leads with what's in flight.
        jobs = sorted(snapshot.jobs, key=lambda j: (j.is_terminal, j.status != "running"))
        self._render_jobs(jobs[: self.MAX_BARS])

    def _render_jobs(self, jobs: list) -> None:
        # Reuse bar widgets; grow/shrink the pool to match the visible job count.
        while len(self._bars) < len(jobs):
            bar = JobBar(self)
            self._bars.append(bar)
            self._bars_box.addWidget(bar)
        for index, bar in enumerate(self._bars):
            if index < len(jobs):
                bar.set_job(jobs[index])
                bar.setVisible(True)
            else:
                bar.setVisible(False)
        self._empty_label.setVisible(not jobs)


class MonitorSummaryDockWidget(MSDockWidget):
    open_requested = Signal()

    def __init__(
        self,
        title: str,
        manager: DockManager,
        *,
        bridge: MolSuiteMonitorBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, manager, parent)
        self.bridge = bridge
        self.summary_widget = MonitorSummaryWidget(bridge=bridge, parent=self)
        self.summary_widget.open_requested.connect(self.open_requested.emit)
        self.setWidget(self.summary_widget)

    @property
    def icon(self):
        return load_icon("activity.svg")

    def refresh_now(self):
        return self.bridge.refresh_now()


class MonitorPage(QWidget):
    def __init__(self, *, bridge: MolSuiteMonitorBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.monitor_widget = MolSuiteMonitorWidget(bridge=bridge, parent=self)
        layout.addWidget(self.monitor_widget)
