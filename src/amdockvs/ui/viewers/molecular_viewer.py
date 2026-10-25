"""Everything that puts a structure on the PyMOL canvas: receptors, molecules, binding
sites, complexes and docking poses, plus the grid-box side panel that travels with them."""

from __future__ import annotations

import contextlib
import logging
import math

from amdockvs.core.payloads import molecule_payload_id, payload_store
from amdockvs.project.summaries import DockingHitSummary
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.tools.pymol_ribbon import (
    PLDDT_BANDS,
    apply_ligand_atom_coloring,
    apply_receptor_atom_coloring,
    apply_receptor_ligand_atom_coloring,
    apply_scene_atom_coloring,
    fetch_structures,
    load_structure,
    multiple_selection_enabled,
    prepare_pymol_scene,
    set_pymol_scene_context,
)

_log = logging.getLogger(__name__)


def _frame(cmd, selection: str, buffer: float = 2.0) -> None:
    """Move the camera so `selection` fits the panel in both directions. PyMOL's zoom fits the
    height only, so a tall, narrow dock cut the sides off and the molecule looked missing."""
    import numpy as np

    xyz = cmd.get_coords(selection)
    width, height = cmd.get_viewport()
    if xyz is None or width <= 0 or height <= 0:
        return
    view = list(cmd.get_view())
    camera = (xyz - np.asarray(view[12:15])) @ np.reshape(view[:9], (3, 3))  # model -> camera axes
    half_x, half_y = np.abs(camera[:, :2]).max(axis=0) + buffer
    tan = math.tan(math.radians(float(cmd.get("field_of_view")) / 2))
    distance = float(max(half_y, half_x * height / width) / tan + np.abs(camera[:, 2]).max())
    shift = distance - abs(view[11])
    view[11] = -distance  # negative: the camera looks down -z at the origin
    view[15] += shift  # the clipping planes are measured from the camera
    view[16] += shift
    cmd.set_view(view)



class MolecularViewerController:
    def __init__(self, window):
        self.w = window
        self.grid_preview_enabled = True
        self._request = 0  # single selection keeps only the latest read; multiple selection keeps all

    # -- reading structures --------------------------------------------------------

    def _later(self, work, then) -> None:
        """Read off the GUI thread; discard superseded single selections or a cleared scene."""
        if not multiple_selection_enabled(self.w.pymol_dock):
            self._request += 1
        request = self._request

        def done(result) -> None:
            if request == self._request:
                then(result)

        run_async(work, done, lambda error: _log.warning("PyMOL could not read its structures: %s", error))

    def set_multiple_selection_enabled(self, enabled: bool) -> None:
        self._request += 1
        dock = self.w.pymol_dock
        dock._amdock_multiple_selection = bool(enabled)
        dock._amdock_scene_epoch = getattr(dock, "_amdock_scene_epoch", 0) + 1
        self._replace_next_pose = not enabled

    def clear_scene(self) -> None:
        self._request += 1
        dock = self.w.pymol_dock
        if dock is not None and dock.cmd is not None:
            dock._amdock_scene_epoch = getattr(dock, "_amdock_scene_epoch", 0) + 1
            try:
                dock.cmd.delete("all")
                dock._amdock_replaced_slots = {}
                set_pymol_scene_context(dock, "generic")
            except Exception as error:
                _log.warning("PyMOL could not clear its scene: %s", error)

    def _read(self, wanted: dict, then) -> None:
        """`then({name: (bytes, kind)})` for the payload ids in `wanted` that are set."""
        ids = {name: int(payload_id) for name, payload_id in wanted.items() if payload_id}
        if not ids:
            if not multiple_selection_enabled(self.w.pymol_dock):
                self._request += 1
            then({})
            return
        store = payload_store(self.w.runtime)
        self._later(lambda: fetch_structures(store, ids), then)

    # -- grid box ------------------------------------------------------------------

    def set_grid_preview_enabled(self, enabled: bool) -> None:
        self.grid_preview_enabled = bool(enabled)
        if self.w.grid_dock is not None:
            self.w.grid_dock.set_auto_preview_enabled(self.grid_preview_enabled)
        if not self.grid_preview_enabled:
            self.hide_grid_panel()

    def show_grid_panel(self) -> None:
        if self.w.pymol_dock is not None:
            self.w.pymol_dock.set_side_panel_visible(True)

    def hide_grid_panel(self) -> None:
        if self.w.pymol_dock is not None:
            self.w.pymol_dock.set_side_panel_visible(False)

    # -- receptors -----------------------------------------------------------------

    @staticmethod
    def _receptor_pymol_name(receptor) -> str:
        return f"receptor_{int(getattr(receptor, 'id', 0) or 0)}"

    def focus_receptor_in_pymol(self, receptor) -> None:
        """Load a receptor into PyMOL and draw its active binding-site box.

        Used by Docking Studio when a receptor is marked: structure + grid box show together.
        PyMOL is a hard dependency, so pymol_dock/grid_dock are assumed present.
        """
        if receptor is None or not bool(getattr(receptor, "is_receptor", False)):
            return
        # Details + grid side panel (set_molecule, show_grid_panel).
        self.w.aux.show_catalog_selection_details("receptor", receptor)
        if self.w.pymol_dock is None:  # headless / PyMOL failed to load: details panel is enough
            return
        self._read({"receptor": molecule_payload_id(receptor)}, lambda found: self._show_receptor(receptor, found))

    def _show_receptor(self, receptor, found: dict) -> None:
        cmd = self.w.pymol_dock.cmd
        if "receptor" in found:
            name = self._receptor_pymol_name(receptor)
            try:
                self.w.pymol_dock.show()
                prepare_pymol_scene(self.w.pymol_dock, name)
                load_structure(cmd, found["receptor"], name)
                apply_receptor_atom_coloring(cmd, name)
                cmd.zoom("all", 3)
                cmd.orient(name)
                set_pymol_scene_context(
                    self.w.pymol_dock,
                    "receptor",
                    target=name,
                    selections={"receptor": name},
                    default_preset="amdockvs.receptor",
                )
            except Exception:
                pass
        # Draw the box AFTER loading the structure (delete('all') above would wipe it otherwise).
        active_id = int(self.w.runtime.binding_sites.active_site_id(int(receptor.id or 0)) or 0)
        if active_id > 0:
            self.w.grid_dock.focus_binding_site(receptor, site_id=active_id, ensure_selected=True)
            geometry = self.w.grid_dock.active_box_geometry()
            if geometry is not None:
                self._orient_camera_to_box(cmd, geometry[0], geometry[1])

    @staticmethod
    def _orient_camera_to_box(cmd, center, size, *, axis: str = "x") -> None:
        """Point the camera at the active box down one axis (default +x) so a box face fronts the
        viewer, framed to the box. ponytail: canonical +x view via set_view; if a face lands
        mirrored, flip the sign row here — needs one visual check."""
        cx, cy, cz = (float(center[0]), float(center[1]), float(center[2]))
        span = max(1.0, float(max(size)))
        distance = span * 2.0 + 5.0
        # Rotation looking down +x: world +x -> toward viewer (+z), +y right, +z up.
        rotations = {
            "x": (0.0, 0.0, -1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0),
            "y": (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0),
            "z": (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        }
        rotation = rotations.get(str(axis).lower(), rotations["x"])
        view = (
            *rotation,
            0.0, 0.0, -distance,
            cx, cy, cz,
            max(1.0, distance - span), distance + span, 0.0,
        )
        with contextlib.suppress(Exception):
            cmd.set_view(view)

    def highlight_receptor_residue(self, receptor_id: int, chain: str, resnum: int) -> None:
        """Show+color a single residue as sticks on the loaded receptor (flexible-residue pick)."""
        cmd = self.w.pymol_dock.cmd
        selection = f"receptor_{int(receptor_id or 0)}"
        if str(chain or "").strip():
            selection += f" and chain {chain}"
        selection += f" and resi {int(resnum)}"
        try:
            cmd.select("amdock_flex_sel", f"({selection})")
            cmd.show("sticks", "amdock_flex_sel")
            cmd.color("orange", "amdock_flex_sel")
            cmd.zoom("amdock_flex_sel", 5)
        except Exception:
            return

    def show_diagram_residue(self, hit: DockingHitSummary, key: str, diagram) -> None:
        """A residue picked in the 2D diagram: sticks, zoom and its contacts drawn in 3D."""
        from amdockvs.ui.viewers.interactions_3d import show_residue_interactions

        self._on_pose(hit, lambda cmd, receptor_obj, ligand_obj: show_residue_interactions(
            cmd, receptor_obj=receptor_obj, ligand_obj=ligand_obj, key=key, diagram=diagram))

    def show_interaction(self, hit: DockingHitSummary, contact) -> None:
        """One contact picked in Selected Result's interaction table, drawn in its type's colour."""
        from amdockvs.ui.viewers.interactions_3d import show_interaction

        self._on_pose(hit, lambda cmd, receptor_obj, ligand_obj: show_interaction(
            cmd, receptor_obj=receptor_obj, ligand_obj=ligand_obj, contact=contact))

    def _pose_objects(self, hit) -> tuple[str, str]:
        receptor = f"receptor_{hit.receptor_id}"
        ligand = "amdock_result_pose"
        if multiple_selection_enabled(self.w.pymol_dock):
            suffix = f"_pose_{hit.pose_payload_id or hit.ligand_payload_id}"
            receptor += suffix
            ligand += f"_{hit.receptor_id}{suffix}"
        return receptor, ligand

    def _on_pose(self, hit: DockingHitSummary, draw) -> None:
        cmd = getattr(self.w.pymol_dock, "cmd", None)
        receptor_obj, ligand_obj = self._pose_objects(hit)
        if cmd is None or not self._holds(cmd, receptor_obj) or not self._holds(cmd, ligand_obj):
            return  # ponytail: docked poses only; a redocking hit loads under other names
        with contextlib.suppress(Exception):
            draw(cmd, receptor_obj, ligand_obj)

    def sync_to_active_view(self, view_id: str | None) -> None:
        """PyMOL follows the active tab: a structural view shows its first row; any other
        view (or an empty table) clears PyMOL so it never keeps showing a stale molecule."""
        if self.w.pymol_dock is None:
            return
        widget = self.w.central_widget.open_view(view_id) if view_id else None
        hook = getattr(widget, "show_active_in_pymol", None)
        if callable(hook):
            hook()
            return
        if not multiple_selection_enabled(self.w.pymol_dock):
            self.clear_scene()

    # -- docking poses -------------------------------------------------------------

    def load_hit(self, hit: DockingHitSummary, pose_rank: int = 1) -> None:
        dock = self.w.pymol_dock
        if dock is None or getattr(dock, "cmd", None) is None:
            return
        if getattr(self, "_replace_next_pose", False):
            self.clear_scene()
            self._replace_next_pose = False
        if str(getattr(hit, "run_kind", "") or "").strip().lower() == "redocking" and (
            hit.pose_payload_id or hit.reference_ligand_payload_id or hit.ligand_payload_id
        ):
            self._load_redocking_hit(dock, hit)
            return
        self._load_docked_hit(dock, hit)

    def _load_docked_hit(self, dock, hit: DockingHitSummary, *, with_receptor: bool = False) -> None:
        cmd = dock.cmd
        receptor_obj, ligand_obj = self._pose_objects(hit)
        # Results are grouped by receptor, so load it ONCE: only read and reload it when it
        # changes, then just swap the ligand pose. Ask PyMOL what it actually holds instead of
        # remembering what we loaded: switching tabs runs cmd.delete("all") (sync_to_active_view),
        # and a cached "receptor N is already loaded" flag then hides the receptor for the rest of
        # the session.
        wanted = {"pose": hit.pose_payload_id or hit.ligand_payload_id}  # else the undocked input
        if with_receptor or not self._holds(cmd, receptor_obj):
            wanted["receptor"] = hit.receptor_payload_id

        def show(found: dict) -> None:
            try:
                dock.show()
                if not self._holds(cmd, receptor_obj):
                    if "receptor" not in found and hit.receptor_payload_id and not with_receptor:
                        # Cleared while the pose was being read: ask again, receptor included.
                        self._load_docked_hit(dock, hit, with_receptor=True)
                        return
                    prepare_pymol_scene(dock, receptor_obj, ligand_obj)
                    if "receptor" in found:
                        load_structure(cmd, found["receptor"], receptor_obj)
                        apply_receptor_atom_coloring(cmd, receptor_obj)
                        cmd.zoom(receptor_obj, 3)
                else:
                    cmd.delete(ligand_obj)  # keep the receptor; replace only the pose
                    from amdockvs.ui.viewers.interactions_3d import interaction_group
                    cmd.delete(interaction_group(receptor_obj, ligand_obj))
                if "pose" in found:
                    load_structure(cmd, found["pose"], ligand_obj)  # each pose is its own payload: one state
                    try:
                        cmd.show("sticks", ligand_obj)
                        apply_ligand_atom_coloring(cmd, ligand_obj)
                        cmd.zoom(ligand_obj, 5)
                    except Exception:
                        pass
                selections = {"receptor": receptor_obj}
                if "pose" in found:
                    selections["ligand"] = ligand_obj
                set_pymol_scene_context(
                    dock,
                    "docking_pose",
                    target="all",
                    selections=selections,
                    default_preset="amdockvs.complex",
                )
            except Exception:
                return

        self._read(wanted, show)

    @staticmethod
    def _holds(cmd, name: str) -> bool:
        try:
            return name in set(cmd.get_names("objects"))
        except Exception:
            return False

    def _load_redocking_hit(self, dock, hit: DockingHitSummary) -> None:
        cmd = dock.cmd
        # The same payload docked and measured against: one receptor on the canvas, not two.
        same_receptor = bool(hit.reference_receptor_payload_id) and (
            hit.reference_receptor_payload_id == hit.receptor_payload_id
        )
        wanted = {
            "pose": hit.pose_payload_id,
            "reference_ligand": hit.reference_ligand_payload_id or hit.ligand_payload_id,
            "reference_receptor": hit.reference_receptor_payload_id,
            "receptor": hit.receptor_payload_id,
        }

        def show(found: dict) -> None:
            try:
                dock.show()
                suffix = (f"_{hit.pose_payload_id or hit.reference_ligand_payload_id or hit.ligand_payload_id}"
                          if multiple_selection_enabled(dock) else "")
                reference_receptor_obj = f"redock_reference_receptor{suffix}"
                docking_receptor_obj = f"redock_docking_receptor{suffix}"
                reference_ligand_obj = f"redock_reference_ligand{suffix}"
                docked_pose_obj = f"redock_docked_pose{suffix}"
                prepare_pymol_scene(dock, reference_receptor_obj, docking_receptor_obj,
                                    reference_ligand_obj, docked_pose_obj)
                receptor_objs: list[str] = []
                if "reference_receptor" in found:
                    load_structure(cmd, found["reference_receptor"], reference_receptor_obj)
                    apply_receptor_atom_coloring(cmd, reference_receptor_obj)
                    receptor_objs.append(reference_receptor_obj)
                if "receptor" in found and not same_receptor:
                    if "reference_receptor" in found:
                        load_structure(cmd, found["receptor"], docking_receptor_obj)
                        apply_receptor_atom_coloring(cmd, docking_receptor_obj)
                        receptor_objs.append(docking_receptor_obj)
                        with contextlib.suppress(Exception):
                            cmd.align(docking_receptor_obj, reference_receptor_obj)
                    else:
                        load_structure(cmd, found["receptor"], reference_receptor_obj)
                        apply_receptor_atom_coloring(cmd, reference_receptor_obj)
                        receptor_objs.append(reference_receptor_obj)
                ligand_objs: list[str] = []
                if "reference_ligand" in found:
                    load_structure(cmd, found["reference_ligand"], reference_ligand_obj)
                    ligand_objs.append(reference_ligand_obj)
                if "pose" in found:
                    load_structure(cmd, found["pose"], docked_pose_obj)  # each pose is its own payload: one state
                    ligand_objs.append(docked_pose_obj)
                for obj in ligand_objs:
                    with contextlib.suppress(Exception):
                        cmd.show("sticks", obj)
                        # Reference ligand takes the receptor's coloring so it reads as "the known
                        # answer in context" and the docked pose (ligand coloring) is the one that
                        # stands out. ponytail: exact palette TBD — reusing the two existing schemes.
                        if obj == reference_ligand_obj:
                            apply_receptor_atom_coloring(cmd, obj)
                        else:
                            apply_ligand_atom_coloring(cmd, obj)
                # Orient on the ligand zone but pull back (generous buffer) so the pose sits in
                # context, not literally in the viewer's face.
                if ligand_objs:
                    ligand_sel = " or ".join(ligand_objs)
                    with contextlib.suppress(Exception):
                        cmd.orient(ligand_sel)
                        cmd.zoom(ligand_sel, 8.0)
                else:
                    with contextlib.suppress(Exception):
                        cmd.zoom("all", 3)
                set_pymol_scene_context(
                    dock,
                    "redocking",
                    target="all",
                    selections={
                        "receptor": " or ".join(receptor_objs) or "polymer.protein",
                        "ligand": " or ".join(ligand_objs),
                    },
                    default_preset="amdockvs.complex",
                    # Every complex loads under the same object names, so a remembered camera
                    # belongs to whichever complex was shown before, in its own frame.
                    restore_view=False,
                )
            except Exception:
                return

        self._read(wanted, show)

    # -- shown from the Details panel ----------------------------------------------

    def show_payload(self, payload_id: int, object_name: str) -> None:
        """One stored structure, alone on the canvas."""
        dock = self.w.pymol_dock
        if dock is None or getattr(dock, "cmd", None) is None or not payload_id:
            return
        loaded_object = str(object_name or "selected_file")
        if multiple_selection_enabled(dock):
            loaded_object += f"_{payload_id}"

        def show(found: dict) -> None:
            if "structure" not in found:
                return
            cmd = dock.cmd
            try:
                dock.show()
                prepare_pymol_scene(dock, loaded_object)
                load_structure(cmd, found["structure"], loaded_object)
                apply_scene_atom_coloring(self.w)
                cmd.zoom("all", 3)
                set_pymol_scene_context(
                    dock,
                    "generic",
                    target=loaded_object,
                    selections={"molecule": loaded_object},
                )
            except Exception:
                return

        self._read({"structure": payload_id}, show)

    def show_catalog_molecule(
        self,
        molecule,
        *,
        source: str,
        object_prefix: str,
        details_kind: str,
        orient: bool,
    ) -> None:
        """Show a catalog row while preserving its established object name policy."""
        self.w.aux.show_catalog_selection_details(details_kind, molecule)
        self._show_molecule(
            molecule,
            source=source,
            object_prefix=object_prefix,
            orient=orient,
        )

    def show_molecule(self, molecule, mode: str) -> None:
        self._show_molecule(
            molecule,
            source="current" if str(mode or "").strip().lower() == "current" else "stored",
            object_prefix="molecule",
            orient=True,
        )

    def show_plddt(self, molecule) -> None:
        if molecule is None:
            return
        self._show_molecule(molecule, source="current", object_prefix="molecule", orient=True,
                            then=self._color_plddt)

    def _color_plddt(self, molecule) -> None:
        cmd = getattr(self.w.pymol_dock, "cmd", None)
        if cmd is None:
            return
        name = f"molecule_{getattr(molecule, 'id', 'selected')}"
        for color_name, rgb, condition, _legend in PLDDT_BANDS:
            cmd.set_color(color_name, [value / 255 for value in rgb])
            cmd.color(color_name, f"{name} and {condition}")

    @staticmethod
    def _structure_id(molecule, source: str) -> int | None:
        """The payload a molecule shows for `source`: "stored" is the input as imported when the
        molecule kept it, anything else its working form."""
        stored = getattr(molecule, "stored_payload_id", None)
        return (stored or molecule_payload_id(molecule)) if source == "stored" else molecule_payload_id(molecule)

    def _show_molecule(
        self,
        molecule,
        *,
        source: str,
        object_prefix: str,
        orient: bool,
        then=None,
    ) -> None:
        payload_id = self._structure_id(molecule, str(source or "").strip().lower())
        if not payload_id:
            _log.warning("Molecule %s has no %s structure stored", getattr(molecule, "id", None), source)
            return
        dock = self.w.pymol_dock
        if dock is None or getattr(dock, "cmd", None) is None:
            return
        object_name = f"{object_prefix}_{getattr(molecule, 'id', 'selected')}"

        def show(found: dict) -> None:
            cmd = dock.cmd
            try:
                dock.show()
                prepare_pymol_scene(dock, object_name)
                load_structure(cmd, found["structure"], object_name)
                if bool(getattr(molecule, "is_ligand", False)):
                    context_kind = "ligand"
                    context_role = "ligand"
                    default_preset = "amdockvs.ligand"
                    try:
                        cmd.show("sticks", object_name)
                        apply_ligand_atom_coloring(cmd, object_name)
                    except Exception:
                        pass
                elif bool(getattr(molecule, "is_receptor", False)):
                    context_kind = "receptor"
                    context_role = "receptor"
                    default_preset = "amdockvs.receptor"
                    apply_receptor_atom_coloring(cmd, object_name)
                else:
                    context_kind = "generic"
                    context_role = "molecule"
                    default_preset = ""
                    apply_scene_atom_coloring(self.w)
                cmd.zoom(object_name, 3)
                if orient:
                    try:
                        cmd.orient(object_name)
                        width, height = cmd.get_viewport()
                        if height > width:
                            cmd.turn("z", 90)  # orient lays the long axis across; a tall panel wants it upright
                    except Exception:
                        pass
                _frame(cmd, object_name)
                set_pymol_scene_context(
                    dock,
                    context_kind,
                    target=object_name,
                    selections={context_role: object_name},
                    default_preset=default_preset,
                    # Oriented means framed whole: a view remembered from a zoom (or from Docking
                    # Studio's box camera, same object name) must not replace it.
                    restore_view=not orient,
                )
            except Exception:
                _log.exception("PyMOL could not show %s", object_name)
                return
            # delete("all") above wiped the box: a receptor brings its active site back with it.
            is_receptor = bool(getattr(molecule, "is_receptor", False)) and getattr(self.w, "grid_dock", None)
            active_id = int(self.w.runtime.binding_sites.active_site_id(int(molecule.id or 0)) or 0) if is_receptor else 0
            if active_id:
                self.w.grid_dock.focus_binding_site(molecule, site_id=active_id, ensure_selected=True)
            if then is not None:
                then(molecule)

        self._read({"structure": payload_id}, lambda found: show(found) if "structure" in found else None)

    def show_binding_site(self, molecule, site) -> None:
        dock = self.w.pymol_dock
        if dock is None or getattr(dock, "cmd", None) is None:
            return
        receptor_name = f"receptor_{getattr(molecule, 'id', 'selected')}"

        def show(found: dict) -> None:
            cmd = dock.cmd
            if "receptor" not in found:
                return
            try:
                dock.show()
                prepare_pymol_scene(dock, receptor_name)
                load_structure(cmd, found["receptor"], receptor_name)
                apply_receptor_atom_coloring(cmd, receptor_name)
                cmd.zoom(receptor_name, 3)
                set_pymol_scene_context(
                    dock,
                    "receptor",
                    target=receptor_name,
                    selections={"receptor": receptor_name},
                    default_preset="amdockvs.receptor",
                )
            except Exception:
                return
            if self.w.grid_dock is not None:
                try:
                    self.w.grid_dock.focus_binding_site(
                        molecule,
                        site_id=int(getattr(site, "id", 0) or 0),
                        ensure_selected=True,
                    )
                    self.show_grid_panel()
                except Exception:
                    pass

        self._read({"receptor": molecule_payload_id(molecule)}, show)

    def show_complex(self, pair) -> bool:
        """Start showing a pair; True when PyMOL took the request (it draws once the bytes are read)."""
        dock = self.w.pymol_dock
        if dock is None or getattr(dock, "cmd", None) is None:
            return False
        molecules = self.w.runtime.molecules
        store = payload_store(self.w.runtime)
        pair_id = int(getattr(pair, "id", 0) or 0)

        def work():
            # The pair's snapshots, else the molecules' own forms: all read here, off the GUI thread.
            ligand = molecules.get(int(getattr(pair, "ligand_molecule_id", 0) or 0))
            receptor = molecules.get(int(getattr(pair, "receptor_molecule_id", 0) or 0))
            wanted = {
                "receptor": getattr(pair, "reference_receptor_payload_id", None)
                or (molecule_payload_id(receptor) if receptor is not None else None),
                "ligand": getattr(pair, "reference_ligand_payload_id", None)
                or (self._structure_id(ligand, "stored") if ligand is not None else None),
            }
            ids = {name: int(payload_id) for name, payload_id in wanted.items() if payload_id}
            return (fetch_structures(store, ids) if ids else {}), receptor

        def show(result) -> None:
            found, receptor = result
            if "receptor" not in found:
                return
            cmd = dock.cmd
            try:
                dock.show()
                receptor_obj = f"complex_receptor_{pair_id}"
                ligand_obj = f"complex_ligand_{pair_id}"
                prepare_pymol_scene(dock, receptor_obj, ligand_obj)
                load_structure(cmd, found["receptor"], receptor_obj)
                if "ligand" in found:
                    load_structure(cmd, found["ligand"], ligand_obj)
                    try:
                        cmd.show("sticks", ligand_obj)
                        apply_receptor_ligand_atom_coloring(
                            cmd,
                            receptor_selection=receptor_obj,
                            ligand_selections=[ligand_obj],
                        )
                        cmd.orient(ligand_obj)
                    except Exception:
                        pass
                else:
                    apply_receptor_atom_coloring(cmd, receptor_obj)
                cmd.zoom("all", 3)
                selections = {"receptor": receptor_obj}
                if "ligand" in found:
                    selections["ligand"] = ligand_obj
                set_pymol_scene_context(
                    dock,
                    "complex",
                    target="all",
                    selections=selections,
                    default_preset="amdockvs.complex",
                )
            except Exception:
                return
            if receptor is not None and self.w.grid_dock is not None:
                try:
                    self.w.grid_dock.focus_binding_site(
                        receptor,
                        site_id=int(getattr(pair, "binding_site_id", 0) or 0),
                        ensure_selected=True,
                    )
                    self.show_grid_panel()
                except Exception:
                    pass

        self._later(work, show)
        return True
