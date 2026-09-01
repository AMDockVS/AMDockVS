from pathlib import Path
import sys

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTabBar, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.ui.main_content import MainContentWidget


def test_theme_preserves_native_tab_close_button():
    from ms_components.theme import base_qss

    assert "QTabBar::close-button" not in base_qss()


def test_native_tab_close_button_uses_pointing_cursor():
    app = QApplication.instance() or QApplication(["amdockvs-tab-test"])
    content = MainContentWidget()
    content.register_view("test", "Test", QWidget)

    content.open_or_focus_view("test")
    app.processEvents()

    tab_bar = content.main_content_tabs.tabBar()
    buttons = [
        tab_bar.tabButton(0, QTabBar.ButtonPosition.LeftSide),
        tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide),
    ]
    close_button = next(button for button in buttons if button is not None)

    assert close_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
