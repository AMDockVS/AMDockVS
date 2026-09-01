"""Small, reusable widget helpers.

Anything generic enough that two views could want it, and too small to deserve a module.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def make_placeholder_widget(title: str) -> QWidget:
    """Centered-label stand-in for a view that is registered but not built yet."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(f"{title} content")
    label.setAlignment(Qt.AlignCenter)
    layout.addWidget(label)
    return widget


__all__ = ["make_placeholder_widget"]
