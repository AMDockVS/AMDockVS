"""Shared drag-and-drop file-list bits so the ligand and receptor importers look identical."""
from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QTableWidget, QToolButton, QVBoxLayout, QWidget

from amdockvs.ui.resources.icons import icon


def icon_button(parent, icon_name: str, tooltip: str) -> QToolButton:
    """The add/remove toolbar buttons shared by both import dialogs."""
    button = QToolButton(parent)
    button.setIcon(icon(icon_name))
    button.setToolTip(tooltip)
    return button


def drop_hint(what: str) -> str:
    return f"Drop {what} files here\nor use the Add button"


class TablePlaceholder(QObject):
    """Empty-state overlay for a QTableWidget: a centered hint shown while the table has no rows.

    Lives on the viewport as a mouse-transparent child (so drops pass through) and refreshes
    itself from the model's row signals — no subclassing or manual sync needed.
    """

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
        return False

    def refresh(self, *args) -> None:
        # Model signals can fire while the table is being torn down; skip if it's gone.
        if not shiboken6.isValid(self._table):
            return
        self._widget.setGeometry(self._table.viewport().rect())
        self._widget.setVisible(self._table.model().rowCount() == 0)


__all__ = ["icon_button", "drop_hint", "TablePlaceholder"]
