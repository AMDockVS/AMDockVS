import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from amdockvs.ui.shell.main_content import _WelcomePage


def test_the_welcome_page_paints_a_faint_logo_behind_its_content():
    QApplication.instance() or QApplication([])
    page = _WelcomePage()
    page.resize(400, 300)
    assert not page._logo.pixmap(QSize(64, 64)).isNull()
    image = page.grab().toImage()  # paintEvent runs without raising
    assert not image.isNull()
    assert 0 < page._LOGO_OPACITY <= 0.1  # a watermark, not a picture
