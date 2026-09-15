"""Shared drag-and-drop file-list bits so the ligand and receptor importers look identical."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QLabel, QTableWidget, QToolButton, QVBoxLayout, QWidget

from amdockvs.ui.resources.icons import icon


def icon_button(parent, icon_name: str, tooltip: str) -> QToolButton:
    """The add/remove toolbar buttons shared by both import dialogs."""
    button = QToolButton(parent)
    button.setIcon(icon(icon_name))
    button.setToolTip(tooltip)
    return button


def drop_hint(what: str) -> str:
    return f"Drop {what} files here\nor click to add"


USER_INPUT_SOURCE = "user input"


def write_user_input(text: str, suffix: str) -> str:
    """Typed text → temp file the importer can read.

    ponytail: the temp file is only a carrier; pass its path in ``source_labels`` so the
    molecule's source says "user input" instead of an ephemeral /tmp name.
    """
    fd, path = tempfile.mkstemp(prefix="amdock_input_", suffix=suffix)
    with os.fdopen(fd, "w") as handle:
        handle.write(text.strip() + "\n")
    return str(Path(path).resolve())


def toolbar_separator(parent) -> QFrame:
    """The '|' between the add buttons and remove in the import toolbars."""
    line = QFrame(parent)
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


class TablePlaceholder(QObject):
    """Empty-state overlay for a QTableWidget: a centered hint shown while the table has no rows.

    Lives on the viewport as a mouse-transparent child (so drops pass through) and refreshes
    itself from the model's row signals — no subclassing or manual sync needed.
    While shown, a left click on the empty table emits ``clicked`` (wire it to the Add action).
    """

    clicked = Signal()

    def __init__(self, table: QTableWidget, text: str):
        super().__init__(table)
        self._table = table
        self._widget = QWidget(table.viewport())
        self._widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self._widget)
        layout.setAlignment(Qt.AlignCenter)
        self._icon = QLabel(self._widget)
        self._icon.setAlignment(Qt.AlignCenter)
        label = QLabel(text, self._widget)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: palette(mid); font-size: 22px;")
        layout.addWidget(self._icon)
        layout.addWidget(label)
        self._paint_icon()
        # Mode.Disabled gives the dimmed ink that matches the hint's palette(mid) text.
        # The themed icon resolves its color when the pixmap is made, so re-make it on a
        # theme switch. A child widget gets PaletteChange, not ApplicationPaletteChange.
        self._widget.installEventFilter(self)
        table.viewport().installEventFilter(self)
        model = table.model()
        model.rowsInserted.connect(self.refresh)
        model.rowsRemoved.connect(self.refresh)
        model.modelReset.connect(self.refresh)
        self.refresh()

    def _paint_icon(self) -> None:
        self._icon.setPixmap(
            icon("file-plus.svg").pixmap(QSize(40, 40), QIcon.Mode.Disabled, QIcon.State.Off)
        )

    def eventFilter(self, obj, event) -> bool:
        if not shiboken6.isValid(self._table):
            return False
        if obj is self._widget and event.type() == QEvent.PaletteChange:
            self._paint_icon()
        if obj is self._table.viewport() and event.type() == QEvent.Resize:
            self._widget.setGeometry(self._table.viewport().rect())
        # The overlay stays mouse-transparent so drops reach the viewport; catch the click there.
        if (obj is self._table.viewport() and self._widget.isVisible()
                and event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton):
            self.clicked.emit()
            return True
        return False

    def refresh(self, *args) -> None:
        # Model signals can fire while the table is being torn down; skip if it's gone.
        if not shiboken6.isValid(self._table):
            return
        self._widget.setGeometry(self._table.viewport().rect())
        empty = self._table.model().rowCount() == 0
        self._widget.setVisible(empty)
        self._table.viewport().setCursor(Qt.PointingHandCursor if empty else Qt.ArrowCursor)


__all__ = ["icon_button", "drop_hint", "TablePlaceholder", "USER_INPUT_SOURCE", "write_user_input", "toolbar_separator"]
