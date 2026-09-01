from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QFileDialog, QMessageBox

from amdockvs.ui.resources.icons import icon as load_icon
from ms_components.ms_pymol import PymolPresetSpec, PymolSceneContext

RECEPTOR_CARBON_COLOR = "slate"
LIGAND_CARBON_COLORS = (
    "orange",
    "tv_green",
    "cyan",
    "magenta",
    "yellow",
    "salmon",
    "violet",
    "lime",
    "deepteal",
    "hotpink",
)
HETEROATOM_COLORS = {
    "O": "red",
    "N": "blue",
    "S": "yellow",
    "P": "orange",
    "F": "cyan",
    "Cl": "green",
    "Br": "firebrick",
    "I": "purple",
    "H": "white",
}

# Binding-site highlight surface: neutral receptor surface, pocket residues in one bright color.
BINDING_SURFACE_COLOR = "gray90"
BINDING_HIGHLIGHT_COLOR = "hotpink"

# Residue nature → color, for the "by nature" binding-site preset.
RESIDUE_NATURE_COLORS = {
    "red": ("ASP", "GLU"),  # acidic
    "blue": ("ARG", "LYS", "HIS"),  # basic
    "green": ("SER", "THR", "ASN", "GLN", "CYS"),  # polar
    "orange": ("PHE", "TRP", "TYR"),  # aromatic
    "gray70": ("ALA", "VAL", "LEU", "ILE", "MET", "PRO", "GLY"),  # hydrophobic
}

# The rest of PyMOL's Actions > Preset menu. The control bar already registers
# default/simple/ball_and_stick/pretty/publication; these fill in the missing ones so the
# full Actions > Preset set lives in the Presets menu we already have.
PYMOL_EXTRA_PRESETS = (
    ("PyMOL · Simple (no solvent)", "simple_no_solvent"),
    ("PyMOL · B-factor Putty", "b_factor_putty"),
    ("PyMOL · Technical", "technical"),
    ("PyMOL · Ligand Sites (cartoon)", "ligand_cartoon"),
    ("PyMOL · Ligand Sites (surface)", "ligands"),
    ("PyMOL · Pretty (solvent)", "pretty_solv"),
    ("PyMOL · Protein Interface", "protein_interface"),
)

# Contexts where a docking box / pocket is defined and shown — the surface presets apply here.
_SURFACE_PRESET_CONTEXTS = frozenset(
    {"receptor", "complex", "binding_site", "docking_pose", "redocking", "offtarget"}
)


def _pymol_cmd(window):
    dock = getattr(window, "pymol_dock", None)
    if dock is None:
        return None
    return getattr(dock, "cmd", None)


def _show_pymol(window) -> None:
    dock = getattr(window, "pymol_dock", None)
    if dock is not None:
        dock.show()
        dock.raise_()


def _safe_cmd(window, command: str, *args) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    try:
        getattr(cmd, command)(*args)
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not run PyMOL command '{command}':\n{exc}")


def _safe_set(window, name: str, value) -> None:
    _safe_cmd(window, "set", name, value)


def _safe_color(cmd, color: str, selection: str) -> None:
    try:
        cmd.color(color, selection)
    except Exception:
        pass


def apply_atom_coloring(cmd, selection: str, carbon_color: str) -> None:
    """Color carbons by role while keeping heteroatoms element-colored."""
    selection = str(selection or "").strip()
    if not selection:
        return
    scoped = f"({selection})"
    _safe_color(cmd, carbon_color, f"{scoped} and elem C")
    for element, color in HETEROATOM_COLORS.items():
        _safe_color(cmd, color, f"{scoped} and elem {element}")


def apply_receptor_atom_coloring(cmd, selection: str) -> None:
    apply_atom_coloring(cmd, selection, RECEPTOR_CARBON_COLOR)


def apply_ligand_atom_coloring(cmd, selection: str, index: int = 0) -> None:
    color = LIGAND_CARBON_COLORS[int(index or 0) % len(LIGAND_CARBON_COLORS)]
    apply_atom_coloring(cmd, selection, color)


def color_by_residue_nature(cmd, selection: str) -> None:
    """Color residues by chemical nature (acidic/basic/polar/aromatic/hydrophobic)."""
    selection = str(selection or "").strip()
    if not selection:
        return
    scoped = f"({selection})"
    for color, resns in RESIDUE_NATURE_COLORS.items():
        _safe_color(cmd, color, f"{scoped} and resn {'+'.join(resns)}")


def _object_list(cmd, selection: str) -> list[str]:
    get_object_list = getattr(cmd, "get_object_list", None)
    if not callable(get_object_list):
        return []
    try:
        return [str(name) for name in (get_object_list(selection) or []) if str(name).strip()]
    except Exception:
        return []


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
        ligands = _object_list(cmd, "organic and not solvent")
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
) -> None:
    setter = getattr(dock, "set_scene_context", None)
    if callable(setter):
        setter(
            kind,
            target=target,
            selections=selections,
            default_preset=default_preset,
        )
    _apply_scene_memory(dock, kind, target, selections)


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
    return _select_atoms(cmd, name, f"byres (({receptor}) and ({' or '.join(parts)}))")


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
    if pockets and _select_atoms(cmd, name, f"byres (({receptor}) within 5 of ({pockets}))"):
        return receptor, name
    ligand = context.selection("ligand", "")
    if ligand and _select_atoms(cmd, name, f"byres (({receptor}) within 5 of ({ligand}))"):
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
            contexts=frozenset({"complex", "docking_pose", "redocking", "offtarget"}),
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
    for label, method in PYMOL_EXTRA_PRESETS:
        register(
            PymolPresetSpec(
                key=f"pymol.{method}",
                label=label,
                callback=_pymol_builtin_callback(method),
                tooltip="PyMOL Actions > Preset.",
            )
        )
    window._amdock_pymol_presets_installed = True


def _active_selection_or_all(cmd) -> str:
    try:
        if int(cmd.count_atoms("sele") or 0) > 0:
            return "sele"
    except Exception:
        pass
    return "all"


def _apply_representation(window, representation: str) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    try:
        target = _active_selection_or_all(cmd)
        cmd.hide("everything", target)
        cmd.show(representation, target)
        if representation == "sticks":
            cmd.show("lines", "solvent")
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not apply representation '{representation}':\n{exc}")


def _select_atoms(cmd, name: str, selection: str) -> bool:
    try:
        cmd.select(name, selection)
        count = cmd.count_atoms(name)
        return True if count is None else int(count) > 0
    except Exception:
        return False


def _prepare_complex_selections(cmd) -> tuple[str, str]:
    protein_selection = "amdock_complex_protein"
    ligand_selection = "amdock_complex_ligand"
    _select_atoms(cmd, protein_selection, "polymer.protein")
    if not _select_atoms(cmd, ligand_selection, "organic and not solvent"):
        _select_atoms(cmd, ligand_selection, "not polymer.protein and not solvent")
    return protein_selection, ligand_selection


def _set_object_transparency(cmd, selection: str, value: float) -> None:
    try:
        cmd.set("transparency", float(value), selection)
        return
    except Exception:
        pass
    set_transparency = getattr(cmd, "set_transparency", None)
    if callable(set_transparency):
        try:
            set_transparency(float(value), selection)
        except Exception:
            pass


def _apply_protein_ligand_preset(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    try:
        protein_selection, ligand_selection = _prepare_complex_selections(cmd)
        cmd.hide("everything", "all")
        cmd.show("cartoon", protein_selection)
        cmd.show("sticks", ligand_selection)
        apply_receptor_atom_coloring(cmd, protein_selection)
        apply_ligand_atom_coloring(cmd, ligand_selection)
        cmd.show("lines", "solvent")
        cmd.set("cartoon_transparency", 0.12, protein_selection)
        cmd.zoom(ligand_selection, 5)
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not apply protein-ligand preset:\n{exc}")


def _focus_ligand(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    try:
        _protein_selection, ligand_selection = _prepare_complex_selections(cmd)
        cmd.show("sticks", ligand_selection)
        cmd.zoom(ligand_selection, 4)
        cmd.orient(ligand_selection)
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not focus ligand:\n{exc}")


def _show_binding_pocket(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    try:
        protein_selection, ligand_selection = _prepare_complex_selections(cmd)
        pocket_selection = "amdock_complex_pocket"
        cmd.select(pocket_selection, f"byres ({protein_selection} within 5 of {ligand_selection})")
        cmd.show("sticks", pocket_selection)
        apply_receptor_atom_coloring(cmd, pocket_selection)
        cmd.show("surface", pocket_selection)
        _set_object_transparency(cmd, pocket_selection, 0.55)
        cmd.zoom(f"({pocket_selection} or {ligand_selection})", 4)
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not show binding pocket:\n{exc}")


def _save_png(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    filename, _selected_filter = QFileDialog.getSaveFileName(
        window,
        "Save PyMOL Image",
        str(Path.home() / "pymol.png"),
        "PNG Images (*.png)",
    )
    if not filename:
        return
    try:
        cmd.png(filename, ray=1)
    except Exception as exc:
        QMessageBox.warning(window, "PyMOL", f"Could not save PNG:\n{exc}")


def _refresh_pymol_widget(window) -> None:
    """Force the embedded GL widget to repaint (e.g. after toggling internal_gui)."""
    dock = getattr(window, "pymol_dock", None)
    widget = getattr(dock, "pymol_widget", None) if dock is not None else None
    if widget is not None:
        try:
            widget.update()
        except Exception:
            pass


def _toggle_internal_gui(window, checked: bool) -> None:
    _safe_set(window, "internal_gui", 1 if checked else 0)
    _refresh_pymol_widget(window)


def _object_names(cmd) -> list[str]:
    try:
        return [str(name) for name in (cmd.get_names("objects") or [])]
    except Exception:
        return []


def _pymol_builtin_callback(method: str):
    """Preset callback that runs a PyMOL Actions > Preset method on the scene target."""
    def apply(cmd, context: PymolSceneContext) -> None:
        from pymol import preset

        getattr(preset, method)(str(context.target or "all"), _self=cmd)

    return apply


def _show_vacuum_electrostatics(window) -> None:
    cmd = _pymol_cmd(window)
    if cmd is None:
        return
    # protein_vacuum_esp needs a single EXISTING object name, not a selection expression.
    objects = _object_list(cmd, "polymer.protein") or _object_names(cmd)
    target = objects[0] if objects else ""
    if not target:
        QMessageBox.warning(window, "PyMOL", "Load a protein object first.")
        return
    reply = QMessageBox.question(
        window,
        "PyMOL",
        f"Generate vacuum electrostatics for '{target}'?\n\n"
        "This can take a while and may briefly freeze the window.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if reply != QMessageBox.Yes:
        return
    try:
        from pymol import util

        # ponytail: runs on the GUI thread; PyMOL's cmd isn't safe to drive from a worker.
        util.protein_vacuum_esp(target, mode=2, quiet=0, _self=cmd)
        _refresh_pymol_widget(window)
    except Exception as exc:
        QMessageBox.warning(
            window,
            "PyMOL",
            f"Could not generate vacuum electrostatics for '{target}':\n{exc}\n\n"
            "The target must be a single protein object.",
        )


def _toggle_grid_panel(window, checked: bool) -> None:
    dock = getattr(window, "pymol_dock", None)
    grid_dock = getattr(window, "grid_dock", None)
    if dock is None or grid_dock is None:
        return
    dock.set_side_panel_visible(bool(checked))


# --- style memory & per-molecule view cache -------------------------------------------------
# Two things persist across molecule switches: the chosen style per molecule *type* ("Keep
# style"), and orientation+style per specific molecule (an LRU cache, max 20 — surfaces just
# recompute, which the user accepted). Returning to a molecule restores where you left it.
# PyMOL's embedded widget exposes no viewport-changed signal, so mouse-release events are the
# useful boundary: they preserve the final camera without calling into PyMOL mid-drag.
_VIEW_CACHE_LIMIT = 20


def _mol_key(kind: str, target: str, selections: dict[str, str] | None) -> str:
    sel = "|".join(f"{k}={v}" for k, v in sorted((selections or {}).items()))
    return f"{kind}::{target}::{sel}"


def _trim_cache(cache: "OrderedDict") -> None:
    while len(cache) > _VIEW_CACHE_LIMIT:
        cache.popitem(last=False)


def _apply_preset(dock, key: str) -> None:
    bar = getattr(dock, "control_bar", None)
    fn = getattr(bar, "apply_preset", None)
    if callable(fn):
        try:
            fn(key)
        except Exception:
            pass  # preset not valid for this context — leave the scene as-is


def _set_view(dock, view) -> None:
    cmd = getattr(dock, "cmd", None)
    if cmd is None:
        return
    try:
        cmd.set_view(view)
    except Exception:
        pass


def _apply_scene_memory(dock, kind: str, target: str, selections: dict[str, str] | None) -> None:
    cache = getattr(dock, "_amdock_scene_cache", None)
    if cache is None:
        return  # toolbar / memory not installed yet
    kind = str(kind or "generic")
    key = _mol_key(kind, target or "all", selections)
    dock._amdock_current_key = key
    dock._amdock_current_kind = kind
    entry = cache.get(key)
    if entry is not None:
        cache.move_to_end(key)
        if entry.get("preset"):
            _apply_preset(dock, entry["preset"])
        if entry.get("view") is not None:
            _set_view(dock, entry["view"])  # restore orientation last, over any preset zoom
        return
    if getattr(dock, "_amdock_keep_style", False):
        preset = dock._amdock_style_by_kind.get(kind)
        if preset:
            _apply_preset(dock, preset)


def _on_preset_applied(dock, key: str) -> None:
    key = str(key or "")
    if not key:
        return
    kind = getattr(dock, "_amdock_current_kind", "")
    styles = getattr(dock, "_amdock_style_by_kind", None)
    if kind and styles is not None:
        styles[kind] = key
    cache = getattr(dock, "_amdock_scene_cache", None)
    ck = getattr(dock, "_amdock_current_key", "")
    if cache is not None and ck:
        entry = cache.get(ck) or {}
        entry["preset"] = key
        cache[ck] = entry
        cache.move_to_end(ck)
        _trim_cache(cache)


def _remember_pymol_view(dock) -> None:
    cache = getattr(dock, "_amdock_scene_cache", None)
    ck = getattr(dock, "_amdock_current_key", "")
    cmd = getattr(dock, "cmd", None)
    if cache is None or not ck or cmd is None:
        return
    try:
        view = cmd.get_view()
    except Exception:
        return
    entry = cache.get(ck) or {}
    entry["view"] = view
    cache[ck] = entry
    cache.move_to_end(ck)
    print("pymol save view")
    _trim_cache(cache)


class _PymolViewMemoryFilter(QObject):
    def __init__(self, dock) -> None:
        super().__init__(dock)
        self._dock = dock
        self._wheel_pending = False

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.MouseButtonPress:
            self._dock._amdock_interacting = True
        elif event.type() == QEvent.MouseButtonRelease:
            self._dock._amdock_interacting = False
            _remember_pymol_view(self._dock)
        elif event.type() == QEvent.Wheel and not self._wheel_pending:
            self._wheel_pending = True
            QTimer.singleShot(0, self._remember_after_wheel)
        return False

    def _remember_after_wheel(self) -> None:
        self._wheel_pending = False
        _remember_pymol_view(self._dock)


def _install_scene_memory(dock) -> None:
    if getattr(dock, "_amdock_scene_cache", None) is not None:
        return
    dock._amdock_scene_cache = OrderedDict()
    dock._amdock_style_by_kind = {}
    dock._amdock_current_key = ""
    dock._amdock_current_kind = ""
    dock._amdock_keep_style = False
    dock._amdock_interacting = False
    bar = getattr(dock, "control_bar", None)
    signal = getattr(bar, "preset_applied", None)
    if signal is not None:
        signal.connect(lambda key: _on_preset_applied(dock, key))
    viewer = getattr(dock, "pymol_widget", None)
    if viewer is not None:
        memory_filter = _PymolViewMemoryFilter(dock)
        viewer.installEventFilter(memory_filter)
        dock._amdock_view_memory_filter = memory_filter


def _menu_action(menu, text: str, handler: Callable[[], None], *, tooltip: str = "") -> QAction:
    action = menu.addAction(text)
    if tooltip:
        action.setToolTip(tooltip)
    action.triggered.connect(lambda _checked=False: handler())
    return action


def install_pymol_toolbar(window) -> None:
    """Add AMDock's PyMOL presets and scene menu to the dock's control bar.

    The quick actions (zoom/orient/rock/representation/scene presets) already live in the
    dock's PymolControlBar; the menu holds the config shortcuts (background, camera, render,
    coloring) and the AMDock grid-box toggle — what the old ribbon category duplicated.
    """
    dock = getattr(window, "pymol_dock", None)
    if dock is None or getattr(window, "_pymol_toolbar_installed", False):
        return
    install_amdock_pymol_presets(window)
    _install_scene_memory(dock)

    control_bar = getattr(dock, "control_bar", None)
    if control_bar is None or not hasattr(control_bar, "add_menu_action"):
        return

    from PySide6.QtWidgets import QMenu

    # "Keep style": carry the chosen preset to the next molecule of the same type.
    toolbar = getattr(control_bar, "toolbar", None)
    if toolbar is not None:
        keep_action = QAction(load_icon("style.svg"), "Keep style", toolbar)
        keep_action.setCheckable(True)
        keep_action.setToolTip("Keep the current style when switching to another molecule of the same type.")
        keep_action.toggled.connect(lambda checked: setattr(dock, "_amdock_keep_style", bool(checked)))
        toolbar.addAction(keep_action)
        window._pymol_keep_style_action = keep_action

    menu = QMenu(control_bar)

    _menu_action(menu, "Clear Scene", lambda: _safe_cmd(window, "delete", "all"),
                 tooltip="Delete all objects from the PyMOL scene.")
    _menu_action(menu, "Color by Role", lambda: apply_scene_atom_coloring(window),
                 tooltip="Color receptor/ligand carbons by role, heteroatoms by element.")

    bg_menu = menu.addMenu("Background")
    _menu_action(bg_menu, "White", lambda: _safe_cmd(window, "bg_color", "white"))
    _menu_action(bg_menu, "Black", lambda: _safe_cmd(window, "bg_color", "black"))
    _menu_action(bg_menu, "Grey", lambda: _safe_cmd(window, "bg_color", "grey20"))

    cam_menu = menu.addMenu("Camera")
    _menu_action(cam_menu, "Orthoscopic", lambda: _safe_set(window, "orthoscopic", 1))
    _menu_action(cam_menu, "Perspective", lambda: _safe_set(window, "orthoscopic", 0))

    render_menu = menu.addMenu("Render")
    _menu_action(render_menu, "Shadows On", lambda: _safe_set(window, "ray_shadows", 1))
    _menu_action(render_menu, "Shadows Off", lambda: _safe_set(window, "ray_shadows", 0))
    _menu_action(render_menu, "Ray", lambda: _safe_cmd(window, "ray"))
    _menu_action(render_menu, "Save PNG…", lambda: _save_png(window))

    surface_menu = menu.addMenu("Surface")
    _menu_action(surface_menu, "Vacuum Electrostatics…", lambda: _show_vacuum_electrostatics(window),
                 tooltip="Generate a charge-colored surface (Generate > Vacuum electrostatics). Slow.")

    complex_menu = menu.addMenu("Complex")
    _menu_action(complex_menu, "Protein–Ligand", lambda: _apply_protein_ligand_preset(window),
                 tooltip="Protein cartoon, ligand sticks, ligand-focused camera.")
    _menu_action(complex_menu, "Focus Ligand", lambda: _focus_ligand(window))
    _menu_action(complex_menu, "Pocket", lambda: _show_binding_pocket(window),
                 tooltip="Residues within 5 Å of the ligand as sticks + transparent surface.")

    menu.addSeparator()
    gui_action = menu.addAction("PyMOL Internal GUI")
    gui_action.setCheckable(True)
    gui_action.setChecked(False)  # dock starts with internal_gui off
    gui_action.setToolTip("Show or hide PyMOL's built-in on-canvas menu/object panel.")
    gui_action.toggled.connect(lambda checked: _toggle_internal_gui(window, checked))

    if getattr(window, "grid_dock", None) is not None:
        menu.addSeparator()
        grid_action = menu.addAction("Grid Box")
        grid_action.setCheckable(True)
        grid_action.setChecked(bool(getattr(dock, "is_side_panel_visible", lambda: False)()))
        grid_action.setToolTip("Show or hide the docking grid-box side panel.")
        grid_action.toggled.connect(lambda checked: _toggle_grid_panel(window, checked))

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
