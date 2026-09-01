"""Ephemeral views share one tab slot (§0.5): opening another replaces it, permanent tabs stay."""
from pathlib import Path
import sys

import pydantic.fields  # noqa: F401 - before PySide6: shiboken breaks pydantic's lazy imports
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.ui.main_content import MainContentWidget


def _content():
    QApplication.instance() or QApplication(["amdockvs-preview-test"])
    content = MainContentWidget()
    content.register_view("perm", "Molecules", QWidget)
    content.register_view("prev.a", "QSAR Models", QWidget, preview=True)
    content.register_view("prev.b", "Binding Sites", QWidget, preview=True)
    return content


def _titles(content):
    tabs = content.main_content_tabs
    return [tabs.tabText(i) for i in range(tabs.count())]


def test_a_second_preview_takes_the_slot_from_the_first():
    content = _content()
    content.open_or_focus_view("perm")
    content.open_or_focus_view("prev.a")
    content.open_or_focus_view("prev.b")
    assert _titles(content) == ["Molecules", "Binding Sites"]
    assert content.open_view("prev.a") is None
    assert content._preview_style.preview_title == "Binding Sites"  # the italic one


def test_closing_the_preview_frees_the_slot():
    content = _content()
    content.open_or_focus_view("prev.a")
    content.close_view("prev.a")
    assert content._preview_open is None
    assert _titles(content) == []
