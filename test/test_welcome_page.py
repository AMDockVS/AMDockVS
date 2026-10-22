import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from ms_components.backdrop import Backdrop

from amdockvs.ui.shell.main_content import MainContentWidget


def test_the_welcome_page_paints_a_faint_logo_behind_its_content():
    QApplication.instance() or QApplication([])
    content = MainContentWidget()  # owns the page: keep it alive
    page = content.welcome
    page.resize(400, 300)
    assert isinstance(page, Backdrop)
    assert not page._icon.pixmap(QSize(64, 64)).isNull()
    assert not page.grab().toImage().isNull()  # paintEvent runs without raising
    assert 0 < page._opacity <= 0.1  # a watermark, not a picture
