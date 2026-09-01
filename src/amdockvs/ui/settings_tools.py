"""Settings ▸ External tools: one row per optional tool, install/remove per row.

Every tool AMDock can provision on demand lives here, so a feature panel only has
to say "not installed, see Settings" instead of shipping its own installer.
Installs are deliberately blocking (modal, own thread): a job submitted against a
half-installed tool is a failure that costs more to clean up than the wait.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ms_components.step_dialog import run_steps_dialog

from amdockvs.external_tools import (
    MANAGED_TOOLS,
    ToolStatus,
    install_steps,
    tool_statuses,
    uninstall_tool,
)
from amdockvs.ui.async_query import run_async


def _human_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024 or unit == "GB":
            return f"{size_bytes:.0f} {unit}" if unit == "B" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return ""


class _ToolRow(QFrame):
    def __init__(self, tool, page: "ExternalToolsPage"):
        super().__init__(page)
        self.tool = tool
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(f"<b>{tool.label}</b> — {tool.purpose}", self)
        title.setWordWrap(True)
        text.addWidget(title)
        self.status_label = QLabel("Checking…", self)
        self.status_label.setWordWrap(True)
        text.addWidget(self.status_label)
        layout.addLayout(text, 1)

        self.install_button = QPushButton("Install", self)
        self.install_button.clicked.connect(lambda: page.install(self.tool.tool_id))
        layout.addWidget(self.install_button)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.clicked.connect(lambda: page.remove(self.tool.tool_id))
        layout.addWidget(self.remove_button)

    def apply(self, status: ToolStatus) -> None:
        size = _human_size(status.size_bytes)
        detail = f"{status.message} ({size} on disk)" if size else status.message
        self.status_label.setText(detail if status.installed else f"{detail} · {self.tool.footprint}")
        self.install_button.setEnabled(not status.installed)
        self.remove_button.setEnabled(status.location is not None)


class ExternalToolsPage(QWidget):
    """Status + installer for every optional external tool."""

    def __init__(self, runtime, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        intro = QLabel(
            "Docking runs without these. Each one unlocks one optional feature and is "
            "installed in its own directory, so removing it cannot break AMDock.",
            self,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        self._rows = {tool.tool_id: _ToolRow(tool, self) for tool in MANAGED_TOOLS}
        for row in self._rows.values():
            content_layout.addWidget(row)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        self.install_all_button = QPushButton("Install all", self)
        self.install_all_button.clicked.connect(self.install_all)
        actions.addWidget(self.install_all_button)
        root.addLayout(actions)

        self.refresh()

    def refresh(self) -> None:
        run_async(
            lambda: tool_statuses(self.runtime),
            self._apply,
            on_error=lambda exc: self._apply_error(exc),
            compact=True,
        )

    def _apply(self, statuses) -> None:
        for status in statuses:
            row = self._rows.get(status.tool_id)
            if row is not None:
                row.apply(status)
        self.install_all_button.setEnabled(any(not status.installed for status in statuses))

    def _apply_error(self, exc: Exception) -> None:
        for row in self._rows.values():
            row.status_label.setText(f"Status unavailable: {exc}")
            row.install_button.setEnabled(True)
            row.remove_button.setEnabled(False)

    def install(self, tool_id: str) -> None:
        self._run_install([tool_id])

    def install_all(self) -> None:
        self._run_install([tool.tool_id for tool in MANAGED_TOOLS])

    def _run_install(self, tool_ids: list[str]) -> None:
        pending = [tool_id for tool_id in tool_ids if self._rows[tool_id].install_button.isEnabled()]
        if not pending:
            return
        steps = [step for tool_id in pending for step in install_steps(self.runtime, tool_id)]
        title = "Install external tools" if len(pending) > 1 else f"Install {self._rows[pending[0]].tool.label}"
        run_steps_dialog(self, title, steps, blocking=True, on_success=self.refresh)
        self.refresh()

    def remove(self, tool_id: str) -> None:
        row = self._rows[tool_id]
        confirm = QMessageBox.question(
            self,
            f"Remove {row.tool.label}",
            f"Delete {row.tool.label} from disk? It can be installed again from this page.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        run_async(
            lambda: uninstall_tool(self.runtime, tool_id),
            lambda message: (row.status_label.setText(str(message)), self.refresh()),
            on_error=lambda exc: QMessageBox.warning(self, "Removal failed", str(exc)),
            busy=row.status_label,
            compact=True,
        )


__all__ = ["ExternalToolsPage"]
