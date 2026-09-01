from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStatusBar, QWidget

from ms_components.ms_monitor.job_cards import GlowBar


class JobsStatusIndicator(QWidget):
    """Status-bar monitor indicator shown whenever no Jobs surface (dock or the
    current Jobs view) is on screen — so there is always a monitor indicator
    visible. Shows 'N jobs active [▮▮▮ ]' while jobs run, or 'Jobs: idle' otherwise.
    Clicking it opens the Jobs monitor."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setToolTip("Open Jobs monitor")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 6, 0)
        row.setSpacing(8)
        self._label = QLabel("", self)
        self._label.setStyleSheet("font-size:11px;")
        self._bar = GlowBar(0.0, "running")
        self._bar.setFixedWidth(90)
        row.addWidget(self._label)
        row.addWidget(self._bar)
        self._visible = False
        self._active = 0
        self._progress = 0.0
        self._attention = False
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def set_state(self, *, active: int, progress: float, visible: bool) -> None:
        self._visible = bool(visible)
        self._active = int(active)
        self._progress = float(progress)
        self._render()

    def set_attention(self, on: bool) -> None:
        """Turn the indicator red on job failure (with the monitor hidden, this is the
        only cue). Cleared when the user opens the monitor."""
        self._attention = bool(on)
        self._render()

    def _render(self) -> None:
        if self._attention:
            self.setVisible(True)
            self._label.setStyleSheet("font-size:11px; color:#e05561;")
            self._label.setText("⚠ Job failed — click to view")
            self._bar.setVisible(False)
            return
        self.setVisible(self._visible)
        if not self._visible:
            return
        self._label.setStyleSheet("font-size:11px;")
        if self._active > 0:
            self._label.setText(f"{self._active} job{'s' if self._active != 1 else ''} active")
            self._bar.set_value(max(0.0, min(100.0, self._progress)), "running")
            self._bar.setVisible(True)
        else:
            self._label.setText("Jobs: idle")
            self._bar.setVisible(False)


class WorkflowStatusIndicator(QLabel):
    """Always-visible 'Workflow: N steps · status' chip; click to open the Workflow view.
    Shown only when the active workflow has steps."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setToolTip("Open the active workflow")
        self.setStyleSheet("font-size:11px; padding:0 8px;")
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def set_state(self, *, steps: int, status: str) -> None:
        self.setVisible(steps > 0)
        if steps > 0:
            self.setText(f"⚙ Workflow: {steps} step{'s' if steps != 1 else ''} · {status}")


class _ClickableChip(QLabel):
    """Clickable statusbar chip, hidden by default."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, *, tooltip: str = "") -> None:
        super().__init__(parent)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setStyleSheet("font-size:11px; padding:0 8px;")
        if tooltip:
            self.setToolTip(tooltip)
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ProjectIndicator(_ClickableChip):
    """Active project; clicking opens the projects browser."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, tooltip="Open the projects browser")

    def set_project(self, name: str | None) -> None:
        name = str(name or "").strip()
        if not name:
            self.clear()
            self.setVisible(False)
            return
        self.setText(f"📁 {name}")
        self.setVisible(True)


class ResourceIndicator(_ClickableChip):
    """CPU (and GPU if present) in use by the local scheduler; click opens the monitor."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, tooltip="Local compute in use — open the Jobs monitor")

    def set_state(self, *, used_cpu: int, total_cpu: int, used_gpu: int = 0, total_gpu: int = 0) -> None:
        total_cpu = max(0, int(total_cpu))
        if total_cpu <= 0:
            self.setVisible(False)
            return
        used_cpu = max(0, min(int(used_cpu), total_cpu))
        parts = [f"⚡ {used_cpu}/{total_cpu} CPU"]
        total_gpu = max(0, int(total_gpu))
        if total_gpu > 0:
            used_gpu = max(0, min(int(used_gpu), total_gpu))
            parts.append(f"{used_gpu}/{total_gpu} GPU")
        self.setText(" · ".join(parts))
        self.setVisible(True)


class BackendIndicator(_ClickableChip):
    """Compute backend (loky=Local, ray=Cluster); click opens the monitor."""

    _LABELS = {"loky": "🖥 Local", "ray": "🖧 Cluster"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, tooltip="Active compute backend")

    def set_backend(self, backend: str | None) -> None:
        key = str(backend or "").strip().lower()
        label = self._LABELS.get(key)
        if label is None:
            self.setVisible(False)
            return
        self.setText(label)
        self.setVisible(True)


class StatusBar(QStatusBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Right (global, permanent): project · backend · resources · workflow · jobs.
        self.project_indicator = ProjectIndicator(self)
        self.addPermanentWidget(self.project_indicator)
        self.backend_indicator = BackendIndicator(self)
        self.addPermanentWidget(self.backend_indicator)
        self.resource_indicator = ResourceIndicator(self)
        self.addPermanentWidget(self.resource_indicator)
        self.workflow_indicator = WorkflowStatusIndicator(self)
        self.addPermanentWidget(self.workflow_indicator)
        self.jobs_indicator = JobsStatusIndicator(self)
        self.addPermanentWidget(self.jobs_indicator)
