"""Notification log: the one place where "something happened" is recorded.

Job started / job failed / import summary used to be three transient popups — miss them and
the information was gone (exactly what makes a filtered-out import impossible to diagnose).
They all post here instead: a bell in the menu bar carries the unread count and drops down the
history. A popup, not a workspace tab — notifications are glanceable, they shouldn't take a tab
slot next to Ligands/Receptors, and click-away dismissal is what the gesture expects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

INFO = "info"
WARNING = "warning"
ERROR = "error"

_ICONS = {INFO: "ℹ", WARNING: "⚠", ERROR: "⛔"}
# ponytail: fixed cap, no persistence across restarts. Store to the project db if the history
# turns out to be worth keeping between sessions.
MAX_ENTRIES = 200


@dataclass(frozen=True)
class Notification:
    title: str
    text: str = ""
    level: str = INFO
    at: datetime = field(default_factory=datetime.now)

    def as_line(self) -> str:
        icon = _ICONS.get(self.level, _ICONS[INFO])
        body = f" — {self.text}" if self.text else ""
        return f"{icon}  {self.at:%H:%M}  {self.title}{body}"


class NotificationLog(QWidget):
    """Newest-first list of everything posted so far. Shown as a drop-down under the bell
    (`Qt.Popup` → closes on click-away); `popup_at` anchors its right edge to the button."""

    cleared = Signal()

    def __init__(self, entries: list[Notification] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self._list = QListWidget(self)
        self._list.setWordWrap(True)
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list, 1)
        row = QHBoxLayout()
        self._empty = QLabel("Nothing to report yet.", self)
        self._empty.setStyleSheet("font-size:11px;")
        row.addWidget(self._empty)
        row.addStretch(1)
        clear = QPushButton("Clear", self)
        clear.clicked.connect(self._on_clear)
        row.addWidget(clear)
        layout.addLayout(row)
        self.set_entries(entries or [])

    def set_entries(self, entries: list[Notification]) -> None:
        self._list.clear()
        for note in reversed(entries):  # newest first
            self._add_item(note)
        self._empty.setVisible(not entries)

    def append(self, note: Notification) -> None:
        self._add_item(note, at_top=True)
        self._empty.setVisible(False)

    def _add_item(self, note: Notification, *, at_top: bool = False) -> None:
        item = QListWidgetItem(note.as_line())  # severity rides on the icon, not on a frozen color
        if at_top:
            self._list.insertItem(0, item)
        else:
            self._list.addItem(item)

    def _on_clear(self) -> None:
        self._list.clear()
        self._empty.setVisible(True)
        self.cleared.emit()

    def popup_at(self, button: QWidget) -> None:
        """Drop down from the bell, right-aligned with it and kept inside the screen."""
        self.resize(460, 340)
        corner = button.mapToGlobal(QPoint(button.width(), button.height()))
        x, y = corner.x() - self.width(), corner.y() + 4
        screen = button.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.width()))
        self.move(x, y)
        self.show()
        self.raise_()


class NotificationBell(QToolButton):
    """Menu-bar bell: icon only when everything is read, icon + unread count otherwise."""

    def __init__(self, icon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("menuBarNotifications")
        self.setIcon(icon)
        self.setToolTip("Notifications")
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(20, 20))
        # Match the theme button next to it: no padding, no border, menu-bar text height.
        self.setStyleSheet(
            "QToolButton { padding: 0; margin: 0; border: none; background: transparent; }"
        )
        self.set_unread(0)

    def set_unread(self, count: int, *, level: str = INFO) -> None:
        count = max(0, int(count))
        self.setText("" if count <= 0 else f" {count}")
        color = {ERROR: "#e05561", WARNING: "#d9a441"}.get(level if count else INFO, "")
        self.setStyleSheet(
            "QToolButton { padding: 0; margin: 0; border: none; background: transparent;"
            f"font-size: 11px; font-weight: 600; {f'color: {color};' if color else ''} }}"
        )
        self.setToolTip("Notifications" if count <= 0 else f"Notifications — {count} unread")
        self.adjustSize()


__all__ = [
    "ERROR",
    "INFO",
    "MAX_ENTRIES",
    "WARNING",
    "Notification",
    "NotificationBell",
    "NotificationLog",
]
