from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from ms_components.ms_monitor import MolSuiteMonitorBridge, MolSuiteMonitorWidget


class JobsDialog(QDialog):
    """The Jobs monitor as its own non-modal window.

    A tab or dock had to share space with the tables and PyMOL and squeezed one of them; the
    tools already show their own progress, so the full monitor is opened on demand. Built once
    and only hidden on close, so reopening it is instant and keeps its scroll and selection.
    """

    def __init__(self, *, bridge: MolSuiteMonitorBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Jobs")
        self.setModal(False)
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.monitor_widget = MolSuiteMonitorWidget(bridge=bridge, parent=self)
        layout.addWidget(self.monitor_widget)
