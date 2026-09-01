from pathlib import Path
import sys

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.ui.tools.pymol_ribbon import _install_scene_memory, set_pymol_scene_context


class _Cmd:
    def __init__(self):
        self.view = (1.0, 2.0, 3.0)
        self.restored = None

    def get_view(self):
        return self.view

    def set_view(self, view):
        self.restored = view


class _Dock(QWidget):
    def __init__(self):
        super().__init__()
        self.pymol_widget = QWidget(self)
        self.cmd = _Cmd()
        self.control_bar = None

    def set_scene_context(self, *_args, **_kwargs):
        pass


def test_scene_camera_is_saved_after_interaction_without_a_polling_timer():
    QApplication.instance() or QApplication(["amdockvs-pymol-memory-test"])
    dock = _Dock()
    _install_scene_memory(dock)
    set_pymol_scene_context(dock, "ligand", target="ligand_1")

    dock._amdock_view_memory_filter.eventFilter(
        dock.pymol_widget,
        QEvent(QEvent.MouseButtonPress),
    )
    assert dock._amdock_interacting is True
    dock._amdock_view_memory_filter.eventFilter(
        dock.pymol_widget,
        QEvent(QEvent.MouseButtonRelease),
    )

    assert dock._amdock_interacting is False
    assert not hasattr(dock, "_amdock_view_timer")
    assert next(iter(dock._amdock_scene_cache.values()))["view"] == (1.0, 2.0, 3.0)
