from types import SimpleNamespace

# pydantic BEFORE PySide6: shiboken's feature loader trips pydantic's lazy migration shim otherwise.
import amdockvs.runtime  # noqa: F401

from amdockvs.ui.main_window import AMDockVSMainWindow


class _Button:
    def __init__(self):
        self.checked = False

    def isChecked(self):
        return self.checked


class _Docks:
    def __init__(self):
        self.buttons = {"diagram": _Button()}

    def toggle(self, dock_id, checked):
        self.buttons[dock_id].checked = checked


def test_the_diagram_leaves_with_the_results_views_and_returns_with_them():
    window = SimpleNamespace(dock_manager=_Docks(), _diagram_parked=False)
    button = window.dock_manager.buttons["diagram"]
    sync = lambda view_id: AMDockVSMainWindow._sync_diagram_to_view(window, view_id)

    button.checked = True
    sync("docking_results")
    assert button.checked
    sync("receptors")
    assert not button.checked and window._diagram_parked
    sync("offtarget")
    assert button.checked and not window._diagram_parked

    button.checked = False  # closed by hand on a results view: stays closed everywhere
    sync("ligands")
    sync("docking_results")
    assert not button.checked
