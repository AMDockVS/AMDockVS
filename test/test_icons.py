from importlib.resources import files

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from amdockvs.ui.resources.icons import icon


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(["amdockvs-icons-test"])


def packaged_svg_names() -> list[str]:
    icons_dir = files("amdockvs.ui.resources.icons")
    return sorted(
        resource.name
        for resource in icons_dir.iterdir()
        if resource.is_file() and resource.name.endswith(".svg")
    )


@pytest.mark.parametrize("icon_name", packaged_svg_names())
def test_every_packaged_icon_loads_and_renders(app, icon_name):
    loaded = icon(icon_name)

    assert not loaded.isNull(), f"Could not load icon: {icon_name}"

    # Themed icons are rendered lazily. pixmap() also catches SVGs that are empty,
    # invalid, or that the QIconEngine cannot render.
    pixmap = loaded.pixmap(QSize(24, 24))
    assert not pixmap.isNull(), f"Could not render icon: {icon_name}"