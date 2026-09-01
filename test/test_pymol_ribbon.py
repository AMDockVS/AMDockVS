from pathlib import Path
import sys

import pytest

pytest.importorskip("PySide6")
# NeoRibbon is an optional external viewer shell, not an AMDockVS dependency.
pytest.importorskip("neoribbon")
from PySide6.QtWidgets import QApplication, QDockWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neoribbon.core.RibbonMainWindow import RibbonMainWindow
from amdockvs.ui.tools.pymol_ribbon import (
    install_pymol_ribbon,
    set_pymol_scene_context,
)
from ms_components.ms_pymol import PymolSceneContext


class FakePyMOLCmd:
    def __init__(self):
        self.calls = []
        self.active_selection_count = 0

    def get_object_list(self, selection):
        self.calls.append(("get_object_list", (selection,), {}))
        if selection == "organic and not solvent":
            return ["ligand_a", "ligand_b"]
        return []

    def count_atoms(self, selection):
        self.calls.append(("count_atoms", (selection,), {}))
        if selection == "sele":
            return self.active_selection_count
        return 10

    def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))
        if args and args[0] == "surface_transparency":
            raise RuntimeError("unknown Setting: 'surface_transparency'")

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return record


class FakePyMOLDock(QDockWidget):
    def __init__(self):
        super().__init__("PyMOL")
        self.cmd = FakePyMOLCmd()
        self.side_panel_visible = False
        self.presets = {}
        self.scene_context = None

    def register_preset(self, spec) -> None:
        self.presets[spec.key] = spec

    def set_scene_context(self, context, **kwargs) -> None:
        self.scene_context = (context, kwargs)

    def set_side_panel_visible(self, visible: bool) -> None:
        self.side_panel_visible = bool(visible)

    def is_side_panel_visible(self) -> bool:
        return self.side_panel_visible


class FakeRibbonWindow(RibbonMainWindow):
    def __init__(self):
        super().__init__()
        self.pymol_dock = FakePyMOLDock()
        self.grid_dock = object()

    def ensure_ribbon_category(self, title: str):
        ribbon = self.ribbonBar()
        category = ribbon.categoryByName(title)
        if category is None:
            category = ribbon.addCategoryPage(title)
        return category


def action_by_name(panel, object_name: str):
    return next(action for action in panel.actions() if action.objectName() == object_name)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_pymol_ribbon_installs_generic_and_amdock_actions():
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    del app
    window = FakeRibbonWindow()
    try:
        install_pymol_ribbon(window)
        assert set(window.pymol_dock.presets) == {
            "amdockvs.receptor",
            "amdockvs.ligand",
            "amdockvs.complex",
            "amdockvs.binding_points",
            "amdockvs.binding_residues",
            "amdockvs.binding_surface",
        }
        context = window._pymol_ribbon_context
        assert context is not None
        category = context.categoryPage(0)
        assert category is not None

        scene_panel = category.panelByName("Scene")
        display_panel = category.panelByName("Display")
        colors_panel = category.panelByName("Colors")
        complex_panel = category.panelByName("Complex")
        amdock_panel = category.panelByName("AMDock")
        assert scene_panel is not None
        assert display_panel is not None
        assert colors_panel is not None
        assert complex_panel is not None
        assert amdock_panel is not None

        action_by_name(scene_panel, "pymol.zoom_all").trigger()
        assert ("zoom", ("all", 3), {}) in window.pymol_dock.cmd.calls

        window.pymol_dock.cmd.active_selection_count = 6
        action_by_name(display_panel, "pymol.display.sticks").trigger()
        assert ("hide", ("everything", "sele"), {}) in window.pymol_dock.cmd.calls
        assert ("show", ("sticks", "sele"), {}) in window.pymol_dock.cmd.calls
        assert ("hide", ("everything", "all"), {}) not in window.pymol_dock.cmd.calls

        action_by_name(colors_panel, "pymol.colors.role_atoms").trigger()
        assert ("color", ("slate", "(polymer.protein) and elem C"), {}) in window.pymol_dock.cmd.calls
        assert ("color", ("orange", "(ligand_a) and elem C"), {}) in window.pymol_dock.cmd.calls
        assert ("color", ("tv_green", "(ligand_b) and elem C"), {}) in window.pymol_dock.cmd.calls
        assert ("color", ("red", "(ligand_a) and elem O"), {}) in window.pymol_dock.cmd.calls

        action_by_name(complex_panel, "pymol.complex.protein_ligand").trigger()
        assert ("select", ("amdock_complex_protein", "polymer.protein"), {}) in window.pymol_dock.cmd.calls
        assert ("select", ("amdock_complex_ligand", "organic and not solvent"), {}) in window.pymol_dock.cmd.calls
        assert ("show", ("cartoon", "amdock_complex_protein"), {}) in window.pymol_dock.cmd.calls
        assert ("show", ("sticks", "amdock_complex_ligand"), {}) in window.pymol_dock.cmd.calls
        assert (
            "color",
            ("slate", "(amdock_complex_protein) and elem C"),
            {},
        ) in window.pymol_dock.cmd.calls
        assert (
            "color",
            ("orange", "(amdock_complex_ligand) and elem C"),
            {},
        ) in window.pymol_dock.cmd.calls
        assert ("zoom", ("amdock_complex_ligand", 5), {}) in window.pymol_dock.cmd.calls

        action_by_name(complex_panel, "pymol.complex.pocket").trigger()
        assert (
            "select",
            ("amdock_complex_pocket", "byres (amdock_complex_protein within 5 of amdock_complex_ligand)"),
            {},
        ) in window.pymol_dock.cmd.calls
        assert ("show", ("surface", "amdock_complex_pocket"), {}) in window.pymol_dock.cmd.calls
        assert ("set", ("transparency", 0.55, "amdock_complex_pocket"), {}) in window.pymol_dock.cmd.calls
        assert all(
            call != ("set", ("surface_transparency", 0.55, "amdock_complex_pocket"), {})
            for call in window.pymol_dock.cmd.calls
        )

        grid_action = action_by_name(amdock_panel, "pymol.amdockvs.grid_box")
        grid_action.setChecked(True)
        assert window.pymol_dock.is_side_panel_visible() is True

        # Contextual category: tab appears only while the PyMOL dock is visible.
        ribbon = window.ribbonBar()
        from amdockvs.ui.tools.pymol_ribbon import _sync_context_visibility

        window.pymol_dock.setVisible(True)
        _sync_context_visibility(window, context)
        assert ribbon.isContextCategoryVisible(context) is True
        window.pymol_dock.setVisible(False)
        _sync_context_visibility(window, context)
        assert ribbon.isContextCategoryVisible(context) is False
    finally:
        window.close()


def test_pymol_scene_context_and_binding_site_preset():
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    del app
    window = FakeRibbonWindow()
    try:
        install_pymol_ribbon(window)
        set_pymol_scene_context(
            window.pymol_dock,
            "binding_site",
            target="pockets or centers",
            selections={
                "receptor": "receptor_8",
                "pockets": "pocket_points",
                "centers": "pocket_centers",
            },
            default_preset="amdockvs.binding_points",
        )
        assert window.pymol_dock.scene_context == (
            "binding_site",
            {
                "target": "pockets or centers",
                "selections": {
                    "receptor": "receptor_8",
                    "pockets": "pocket_points",
                    "centers": "pocket_centers",
                },
                "default_preset": "amdockvs.binding_points",
            },
        )

        spec = window.pymol_dock.presets["amdockvs.binding_residues"]
        spec.callback(
            window.pymol_dock.cmd,
            PymolSceneContext(
                kind="binding_site",
                target="pockets or centers",
                selections={
                    "receptor": "receptor_8",
                    "pockets": "pocket_points",
                    "centers": "pocket_centers",
                },
            ),
        )
        assert (
            "select",
            (
                "amdock_preset_pocket_residues",
                "byres ((receptor_8) within 4 of (pocket_points))",
            ),
            {},
        ) in window.pymol_dock.cmd.calls
        assert (
            "show",
            ("sticks", "amdock_preset_pocket_residues"),
            {},
        ) in window.pymol_dock.cmd.calls
    finally:
        window.close()
