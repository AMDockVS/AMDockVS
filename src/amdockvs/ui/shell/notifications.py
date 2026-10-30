"""Notification log: the one place where "something happened" is recorded.

Job started / job failed / import summary used to be three transient popups — miss them and
the information was gone (exactly what makes a filtered-out import impossible to diagnose).
They all post here instead: a bell in the menu bar carries the unread count and drops down the
history. A popup, not a workspace tab — notifications are glanceable, they shouldn't take a tab
slot next to Ligands/Receptors, and click-away dismissal is what the gesture expects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from datetime import datetime
from importlib.resources import files

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

INFO = "info"
WARNING = "warning"
ERROR = "error"

_ICONS = {INFO: "ℹ", WARNING: "⚠", ERROR: "⛔"}
_COLORS = {WARNING: "#d9a441", ERROR: "#e05561"}
# Lucide icons per severity in ui/resources/icons; the glyph above stands in while one is missing.
_ICON_FILES = {INFO: "info.svg", WARNING: "warning.svg", ERROR: "error.svg"}
# ponytail: fixed cap, no persistence across restarts. Store to the project db if the history
# turns out to be worth keeping between sessions.
MAX_ENTRIES = 200


@dataclass(frozen=True)
class Notification:
    title: str
    text: str = ""
    level: str = INFO
    at: datetime = field(default_factory=datetime.now)
    details: str = ""  # the whole story (a traceback): behind "Show Details" on double-click
    open: Callable[[], object] | None = field(default=None, compare=False)  # double-click goes there, no dialog

    def as_line(self, *, glyph: bool = True) -> str:
        icon = f"{_ICONS.get(self.level, _ICONS[INFO])}  " if glyph else ""
        body = f" - {self.text}" if self.text else ""
        return f"{icon}{self.at:%H:%M}  {self.title}{body}"


def severity_icon(level: str) -> QIcon | None:
    """The severity's SVG, tinted amber/red (info follows the theme); None if the file is missing."""
    from amdockvs.ui.resources import icons
    from amdockvs.ui.resources.icons.themed import themed_icon

    path = files(icons) / _ICON_FILES.get(level, _ICON_FILES[INFO])
    if not path.is_file():
        return None
    color = _COLORS.get(level)
    return themed_icon(str(path), QColor(color)) if color else icons.icon(path.name)


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
        self._list.setToolTip("Double-click an entry to read all of it.")
        self._list.itemDoubleClicked.connect(self._show_entry)
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
        # Severity rides on the icon; the text glyph only stands in for a missing SVG.
        icon = severity_icon(note.level)
        item = QListWidgetItem(note.as_line(glyph=icon is None))
        if icon is not None:
            item.setIcon(icon)
        item.setData(Qt.ItemDataRole.UserRole, note)
        if at_top:
            self._list.insertItem(0, item)
        else:
            self._list.addItem(item)

    def _show_entry(self, item: QListWidgetItem) -> None:
        note: Notification = item.data(Qt.ItemDataRole.UserRole)
        if note.open is not None:
            self.hide()
            note.open()
            return
        icon = {ERROR: QMessageBox.Icon.Critical, WARNING: QMessageBox.Icon.Warning}.get(
            note.level, QMessageBox.Icon.Information
        )
        self.hide()  # a popup: it would close under the dialog anyway
        box = QMessageBox(icon, note.title, note.text or note.title, QMessageBox.StandardButton.Ok,
                          self.parentWidget())
        if note.details:
            box.setDetailedText(note.details)
        box.open()  # not exec: a test or a busy event loop must not block on it
        self.entry_dialog = box

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
        color = _COLORS.get(level if count else INFO, "")
        self.setStyleSheet(
            "QToolButton { padding: 0; margin: 0; border: none; background: transparent;"
            f"font-size: 11px; font-weight: 600; {f'color: {color};' if color else ''} }}"
        )
        self.setToolTip("Notifications" if count <= 0 else f"Notifications - {count} unread")
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
