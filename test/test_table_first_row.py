"""A table brought forward before its rows land still shows its first row when they do."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from amdockvs.runtime import AMDockVSRuntime  # noqa: F401  (before PySide6, see test_build_ui)
from amdockvs.ui.catalog.common import BoundTableWidget


class _Model:
    rows: list = []

    def rowCount(self):
        return len(self.rows)

    def get_raw_object(self, index):
        return self.rows[index]


class _Fake:
    show_active_in_pymol = BoundTableWidget.show_active_in_pymol
    _show_first_when_loaded = BoundTableWidget._show_first_when_loaded

    def __init__(self):
        self._table = type("T", (), {"_model": _Model(), "_table": type("V", (), {"selectRow": lambda s, r: None})()})()
        self._show_first_pending = False
        self.shown, self.cleared = [], 0

    def isVisible(self):
        return True

    def clear_pymol(self):
        self.cleared += 1

    def _load_object_in_pymol(self, obj):
        self.shown.append(obj)


def test_activated_before_loading_shows_the_first_row_once_it_lands():
    view = _Fake()
    view.show_active_in_pymol()  # tab activated, rows not loaded yet
    assert view.shown == [] and view.cleared == 1

    view._table._model.rows = ["ligand 1", "ligand 2"]
    view._show_first_when_loaded(2)
    assert view.shown == ["ligand 1"]

    view._show_first_when_loaded(2)  # a later refresh leaves the user's pick alone
    assert view.shown == ["ligand 1"]
