"""Small shared widget helpers. Nothing here is a component — just Qt spellings we repeat."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy, QToolButton, QWidget


def split_button(text: str, parent: QWidget, *, on_click, primary: bool = False) -> QToolButton:
    """Push button with a menu arrow welded to its right: the body runs the primary action, the
    arrow drops the alternatives. Qt does this natively (MenuButtonPopup) — fill the menu with
    `button.menu().addAction(...)`. `primary=True` makes it the big bold CTA of its step."""
    button = QToolButton(parent)
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonTextOnly)
    button.setPopupMode(QToolButton.MenuButtonPopup)
    # QToolButton's default vertical policy grows; a QPushButton's doesn't. Without this the
    # button stretches to the full height of whatever layout holds it.
    button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    if primary:
        button.setMinimumSize(180, 44)
        font = button.font()
        font.setPointSizeF(font.pointSizeF() + 2.0)
        font.setBold(True)
        button.setFont(font)
    button.clicked.connect(on_click)
    menu = QMenu(button)
    menu.setToolTipsVisible(True)  # off by default in QMenu, and these entries need the why
    button.setMenu(menu)
    return button


def right_aligned(widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(widget)
    return row


__all__ = ["right_aligned", "split_button"]
