from __future__ import annotations

from collections import defaultdict

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from ms_components.ms_pymol import (
    COMPARISON_COLORS,
    PymolPresetSpec,
    PymolSceneContext,
    add_scene_actions,
    color_atoms,
    color_by_residue_nature,
    install_scene_memory,
    menu_action,
    object_list,
    select_atoms,
    set_scene_context,
)
from ms_components.ms_pymol.scene import RECEPTOR_CARBON_COLOR

LIGAND_CARBON_COLORS = COMPARISON_COLORS


# Binding-site highlight surface: neutral receptor surface, pocket residues in one bright color.
# AlphaFold pLDDT bands (name, rgb, B-factor selection, legend); predicted models keep pLDDT 0-100 in B.
PLDDT_BANDS = (
    ("plddt_very_high", (0, 83, 214), "b > 90", "> 90 very high"),
    ("plddt_confident", (101, 203, 243), "b > 70 and b < 90.001", "70–90 confident"),
    ("plddt_low", (255, 219, 19), "b > 50 and b < 70.001", "50–70 low"),
    ("plddt_very_low", (255, 125, 69), "b < 50.001", "< 50 very low"),
)
BINDING_SURFACE_COLOR = "gray90"
ZINC_PSEUDO_ATOM_COLOR = "gray70"
BINDING_HIGHLIGHT_COLOR = "hotpink"

# Contexts where a docking box / pocket is defined and shown — the surface presets apply here.
_SURFACE_PRESET_CONTEXTS = frozenset(
    {"receptor", "complex", "binding_site", "docking_pose", "redocking", "offtarget", "comparison"}
)


# PyMOL's name for a payload kind when it differs.
_PYMOL_FORMATS = {"ent": "pdb", "mmcif": "cif"}


def fetch_structures(store, ids: dict) -> dict:
    """{name: (bytes, kind)} of the payloads in `ids` ({name: payload id}), read in volume order.
    Blocking: call it from `run_async`, never from the GUI thread."""
    found = store.get_many(ids.values())
    kinds = store.kinds(ids.values())
    return {name: (found[payload_id], kinds.get(payload_id, "")) for name, payload_id in ids.items()}


def _pymol_input(structure: tuple) -> tuple:
    """(data, PyMOL format). PyMOL reads no SMILES: a SMILES-only molecule shows as its 2D block."""
    data, kind = structure
    if kind == "smi":
        from rdkit import Chem

        text = data.decode() if isinstance(data, bytes) else str(data)
        return Chem.MolToMolBlock(Chem.MolFromSmiles(text.split()[0])), "mol"
    return data, _PYMOL_FORMATS.get(kind, kind)


def load_structure(cmd, structure: tuple, name: str) -> None:
    """Put a stored structure `(bytes, kind)` on the PyMOL canvas straight from its bytes."""
    cmd.load_raw(*_pymol_input(structure), name)


def replace_comparison_structure(dock, structure: tuple, name: str, *, reference: str, label: str) -> bool:
    return dock.replace_comparison_object(name, *_pymol_input(structure), reference=reference, label=label)


def _pymol_cmd(window):
    dock = getattr(window, "pymol_dock", None)
    if dock is None:
        return None
    return getattr(dock, "cmd", None)


def apply_atom_coloring(cmd, selection: str, carbon_color: str) -> None:
    """Color carbons by role while keeping heteroatoms element-colored."""
    selection = str(selection or "").strip()
    if selection:
        color_atoms(cmd, selection, carbon_color)
        show_zinc_pseudo_atoms(cmd, f"(byobject ({selection})) and name TZ")


def show_zinc_pseudo_atoms(cmd, selection: str) -> None:
    """AutoDock4Zn TZ pseudo-atoms (the zinc method's receptor): large translucent spheres."""
    try:
        if not cmd.count_atoms(selection):
            return
        cmd.show("spheres", selection)
        cmd.set("sphere_scale", 1.0, selection)
        cmd.set("sphere_transparency", 0.5, selection)
        cmd.color(ZINC_PSEUDO_ATOM_COLOR, selection)
    except Exception:
        pass


def apply_receptor_atom_coloring(cmd, selection: str) -> None:
    apply_atom_coloring(cmd, selection, RECEPTOR_CARBON_COLOR)


def apply_ligand_atom_coloring(cmd, selection: str, index: int = 0) -> None:
    color = LIGAND_CARBON_COLORS[int(index or 0) % len(LIGAND_CARBON_COLORS)]
    apply_atom_coloring(cmd, selection, color)


def apply_receptor_ligand_atom_coloring(
    cmd,
    *,
    receptor_selection: str | None = None,
    ligand_selections: list[str] | tuple[str, ...] | None = None,
) -> None:
    receptor = str(receptor_selection or "polymer.protein").strip()
    if receptor:
        apply_receptor_atom_coloring(cmd, receptor)

    ligands = [str(selection).strip() for selection in (ligand_selections or ()) if str(selection).strip()]
    if not ligands:
        ligands = object_list(cmd, "organic and not solvent")
    if not ligands:
        ligands = ["organic and not solvent"]
    for index, ligand in enumerate(ligands):
        apply_ligand_atom_coloring(cmd, ligand, index)


def apply_scene_atom_coloring(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is not None:
        apply_receptor_ligand_atom_coloring(cmd)


def set_pymol_scene_context(
    dock,
    kind: str,
    *,
    target: str = "all",
    selections: dict[str, str] | None = None,
    default_preset: str = "",
    restore_view: bool = True,
    grid_labels: dict[int, str] | None = None,
) -> None:
    """`restore_view=False` keeps the camera the caller just set (an oriented catalog row);
    the molecule's remembered style still applies."""
    set_scene_context(
        dock, kind, target=target, selections=selections, default_preset=default_preset,
        restore_view=restore_view, grid_labels=grid_labels, complex_preset="amdockvs.complex",
    )


def _context_selection(
    context: PymolSceneContext,
    role: str,
    fallback: str,
) -> str:
    return context.selection(role, fallback) or fallback


def _amdock_receptor_preset(cmd, context: PymolSceneContext) -> None:
    receptor = _context_selection(
        context,
        "receptor",
        str(context.target or "polymer.protein"),
    )
    cmd.hide("everything", "all")
    cmd.show("cartoon", receptor)
    cmd.show("lines", f"solvent and ({receptor})")
    cmd.set("cartoon_transparency", 0.08, receptor)
    apply_receptor_atom_coloring(cmd, receptor)
    cmd.zoom(receptor, 3)


def _amdock_ligand_preset(cmd, context: PymolSceneContext) -> None:
    ligand = _context_selection(
        context,
        "ligand",
        str(context.target or "organic and not solvent"),
    )
    cmd.hide("everything", "all")
    cmd.show("sticks", ligand)
    apply_ligand_atom_coloring(cmd, ligand)
    cmd.orient(ligand)
    cmd.zoom(ligand, 4)


def _amdock_complex_preset(cmd, context: PymolSceneContext) -> None:
    receptor = _context_selection(context, "receptor", "polymer.protein")
    ligand = _context_selection(context, "ligand", "organic and not solvent")
    cmd.hide("everything", "all")
    cmd.show("cartoon", receptor)
    cmd.show("sticks", ligand)
    cmd.show("lines", "solvent")
    cmd.set("cartoon_transparency", 0.12, receptor)
    apply_receptor_ligand_atom_coloring(
        cmd,
        receptor_selection=receptor,
        ligand_selections=[ligand],
    )
    cmd.orient(ligand)
    cmd.zoom(ligand, 6)


def _binding_site_parts(
    context: PymolSceneContext,
) -> tuple[str, str, str]:
    return (
        _context_selection(context, "receptor", "polymer.protein"),
        _context_selection(context, "pockets", "amdock_p2rank_pockets"),
        _context_selection(context, "centers", "amdock_p2rank_center_*"),
    )


def _amdock_binding_points_preset(cmd, context: PymolSceneContext) -> None:
    receptor, pockets, centers = _binding_site_parts(context)
    cmd.hide("everything", "all")
    cmd.show("cartoon", receptor)
    cmd.show("spheres", pockets)
    cmd.show("spheres", centers)
    cmd.set("cartoon_transparency", 0.15, receptor)
    cmd.set("sphere_scale", 0.4, pockets)
    cmd.set("sphere_scale", 0.6, centers)
    apply_receptor_atom_coloring(cmd, receptor)
    cmd.zoom(f"({pockets} or {centers})", 5)


def _amdock_binding_residues_preset(cmd, context: PymolSceneContext) -> None:
    receptor, pockets, centers = _binding_site_parts(context)
    residues = "amdock_preset_pocket_residues"
    cmd.select(residues, f"byres (({receptor}) within 4 of ({pockets}))")
    cmd.hide("everything", "all")
    cmd.show("cartoon", receptor)
    cmd.show("sticks", residues)
    cmd.show("spheres", pockets)
    cmd.show("spheres", centers)
    cmd.set("cartoon_transparency", 0.35, receptor)
    apply_receptor_atom_coloring(cmd, receptor)
    apply_receptor_atom_coloring(cmd, residues)
    cmd.zoom(f"({residues} or {pockets})", 5)


def _amdock_binding_surface_preset(cmd, context: PymolSceneContext) -> None:
    receptor, pockets, centers = _binding_site_parts(context)
    residues = "amdock_preset_pocket_surface"
    cmd.select(residues, f"byres (({receptor}) within 5 of ({pockets}))")
    cmd.hide("everything", "all")
    cmd.show("cartoon", receptor)
    cmd.show("surface", residues)
    cmd.show("sticks", residues)
    cmd.show("spheres", pockets)
    cmd.show("spheres", centers)
    cmd.set("cartoon_transparency", 0.55, receptor)
    cmd.set("transparency", 0.45, residues)
    apply_receptor_atom_coloring(cmd, receptor)
    apply_receptor_atom_coloring(cmd, residues)
    cmd.zoom(f"({residues} or {pockets})", 5)


def _gridbox_extent(cmd):
    """((xmin,ymin,zmin),(xmax,ymax,zmax)) of the live 'gridbox' CGO, or None."""
    try:
        if "gridbox" not in (cmd.get_names("objects") or []):
            return None
        return cmd.get_extent("gridbox")
    except Exception:
        return None


def _select_residues_in_box(cmd, receptor: str, extent, name: str) -> bool:
    """Select receptor residues with any atom inside the box extent (the search space)."""
    (lo, hi) = extent
    try:
        model = cmd.get_model(f"({receptor}) and not solvent")
    except Exception:
        return False
    per_chain: dict[str, set[str]] = defaultdict(set)
    for atom in getattr(model, "atom", []) or []:
        x, y, z = atom.coord
        if lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1] and lo[2] <= z <= hi[2]:
            per_chain[atom.chain].add(str(atom.resi))
    if not per_chain:
        return False
    parts = []
    for chain, resis in per_chain.items():
        resi_sel = "+".join(sorted(resis, key=lambda r: (len(r), r)))
        parts.append(f"(chain {chain} and resi {resi_sel})" if chain.strip() else f"(resi {resi_sel})")
    return select_atoms(cmd, name, f"byres (({receptor}) and ({' or '.join(parts)}))")


def _pocket_region_selection(cmd, context: PymolSceneContext, name: str) -> tuple[str, str]:
    """Resolve (receptor, region) where region is the box/pocket residues to highlight.

    Prefers the live grid box (the actual search space), then P2Rank pocket points, then a
    reference ligand. Returns an empty region when nothing localizes the site.
    """
    receptor = _context_selection(context, "receptor", str(context.target or "polymer.protein"))
    extent = _gridbox_extent(cmd)
    if extent is not None and _select_residues_in_box(cmd, receptor, extent, name):
        return receptor, name
    pockets = context.selection("pockets", "")
    if pockets and select_atoms(cmd, name, f"byres (({receptor}) within 5 of ({pockets}))"):
        return receptor, name
    ligand = context.selection("ligand", "")
    if ligand and select_atoms(cmd, name, f"byres (({receptor}) within 5 of ({ligand}))"):
        return receptor, name
    return receptor, ""


def _amdock_binding_highlight_preset(cmd, context: PymolSceneContext) -> None:
    receptor, region = _pocket_region_selection(cmd, context, "amdock_preset_pocket_highlight")
    cmd.hide("everything", "all")
    cmd.show("surface", receptor)
    cmd.color(BINDING_SURFACE_COLOR, receptor)
    if region:
        cmd.color(BINDING_HIGHLIGHT_COLOR, region)
        cmd.show("sticks", region)
        cmd.zoom(region, 5)
    else:
        cmd.zoom(receptor, 3)


def _amdock_binding_nature_preset(cmd, context: PymolSceneContext) -> None:
    receptor, region = _pocket_region_selection(cmd, context, "amdock_preset_pocket_nature")
    cmd.hide("everything", "all")
    cmd.show("surface", receptor)
    cmd.color(BINDING_SURFACE_COLOR, receptor)
    if region:
        cmd.show("sticks", region)
        color_by_residue_nature(cmd, region)
        cmd.zoom(region, 5)
    else:
        cmd.zoom(receptor, 3)


def install_amdock_pymol_presets(window) -> None:
    dock = getattr(window, "pymol_dock", None)
    register = getattr(dock, "register_preset", None)
    if not callable(register) or getattr(window, "_amdock_pymol_presets_installed", False):
        return
    presets = (
        PymolPresetSpec(
            key="amdockvs.receptor",
            label="AMDock · Receptor",
            callback=_amdock_receptor_preset,
            contexts=frozenset({"receptor"}),
            tooltip="Receptor as cartoon with role-aware atom colors.",
        ),
        PymolPresetSpec(
            key="amdockvs.ligand",
            label="AMDock · Ligand",
            callback=_amdock_ligand_preset,
            contexts=frozenset({"ligand"}),
            tooltip="Ligand as sticks, centered and colored by atom role.",
        ),
        PymolPresetSpec(
            key="amdockvs.complex",
            label="AMDock · Protein–ligand",
            callback=_amdock_complex_preset,
            contexts=frozenset({"complex", "docking_pose", "redocking", "offtarget", "comparison"}),
            tooltip="Protein cartoon with ligand or docking pose as sticks.",
        ),
        PymolPresetSpec(
            key="amdockvs.binding_points",
            label="Binding sites · Points",
            callback=_amdock_binding_points_preset,
            contexts=frozenset({"binding_site"}),
            tooltip="P2Rank points and centers over a receptor cartoon.",
        ),
        PymolPresetSpec(
            key="amdockvs.binding_residues",
            label="Binding sites · Residues",
            callback=_amdock_binding_residues_preset,
            contexts=frozenset({"binding_site"}),
            tooltip="Pocket points plus receptor residues within 4 Å.",
        ),
        PymolPresetSpec(
            key="amdockvs.binding_surface",
            label="Binding sites · Surface",
            callback=_amdock_binding_surface_preset,
            contexts=frozenset({"binding_site"}),
            tooltip="Transparent local receptor surface around pocket points.",
        ),
        PymolPresetSpec(
            key="amdockvs.surface_highlight",
            label="Surface · Site highlight",
            callback=_amdock_binding_highlight_preset,
            contexts=_SURFACE_PRESET_CONTEXTS,
            tooltip="Neutral receptor surface with the search-space residues in a highlight color.",
        ),
        PymolPresetSpec(
            key="amdockvs.surface_nature",
            label="Surface · Residue nature",
            callback=_amdock_binding_nature_preset,
            contexts=_SURFACE_PRESET_CONTEXTS,
            tooltip="Receptor surface with search-space residues colored by chemical nature.",
        ),
    )
    for preset in presets:
        register(preset)
    window._amdock_pymol_presets_installed = True


def _toggle_grid_panel(window, checked: bool) -> None:
    dock = getattr(window, "pymol_dock", None)
    grid_dock = getattr(window, "grid_dock", None)
    if dock is None or grid_dock is None:
        return
    dock.set_side_panel_visible(bool(checked))


def _sync_grid_action(action: QAction, visible: bool) -> None:
    if action.isChecked() == bool(visible):
        return
    action.blockSignals(True)
    action.setChecked(bool(visible))
    action.blockSignals(False)


def _color_role(cmd, selection: str, role: str) -> None:
    if role == "receptor":
        apply_receptor_atom_coloring(cmd, selection)
    else:
        apply_ligand_atom_coloring(cmd, selection)


def install_pymol_toolbar(window) -> None:
    """Add AMDock's PyMOL presets and scene menu to the dock's control bar.

    The reusable control bar owns selection, view, display, hide and preset actions. This menu
    holds only AMDock's infrequent scene configuration shortcuts.
    """
    dock = getattr(window, "pymol_dock", None)
    if dock is None or getattr(window, "_pymol_toolbar_installed", False):
        return
    install_amdock_pymol_presets(window)

    control_bar = getattr(dock, "control_bar", None)
    if control_bar is None or not hasattr(control_bar, "add_menu_action"):
        return
    install_scene_memory(dock)

    menu = QMenu(control_bar)
    menu_action(menu, "Clear Scene", window.viewer.clear_scene,
                tooltip="Delete all objects from the PyMOL scene.")
    menu_action(menu, "Color by Role", lambda: apply_scene_atom_coloring(window),
                tooltip="Color receptor/ligand carbons by role, heteroatoms by element.")
    add_scene_actions(menu, dock, color_role=_color_role)

    if getattr(window, "grid_dock", None) is not None:
        grid_action = control_bar.add_action(
            "Grid Box",
            icon_name="box.svg",
            tooltip="Show or hide the docking grid-box side panel.",
            checkable=True,
        )
        grid_action.setChecked(bool(getattr(dock, "is_side_panel_visible", lambda: False)()))
        grid_action.toggled.connect(lambda checked: _toggle_grid_panel(window, checked))
        dock.side_panel_visibility_changed.connect(
            lambda visible: _sync_grid_action(grid_action, visible)
        )
        window._pymol_grid_action = grid_action

    action = control_bar.add_menu_action(
        "Scene",
        menu,
        icon_name="settings.svg",
        tooltip="More PyMOL scene, camera, and render options.",
    )

    window._pymol_overflow_menu = menu  # keep alive
    window._pymol_scene_action = action
    window._pymol_toolbar_installed = True


__all__ = [
    "apply_ligand_atom_coloring",
    "apply_receptor_atom_coloring",
    "apply_receptor_ligand_atom_coloring",
    "apply_scene_atom_coloring",
    "install_amdock_pymol_presets",
    "install_pymol_toolbar",
    "set_pymol_scene_context",
]
