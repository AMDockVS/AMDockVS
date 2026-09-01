"""Everything that puts a structure on the PyMOL canvas: receptors, molecules, binding
sites, complexes and docking poses, plus the grid-box side panel that travels with them."""

from __future__ import annotations

import contextlib
from pathlib import Path

from amdockvs.molecule_paths import current_molecule_path, get_default_project_root, stored_molecule_path
from amdockvs.summaries import DockingHitSummary
from amdockvs.ui.tools.pymol_ribbon import (
    apply_ligand_atom_coloring,
    apply_receptor_atom_coloring,
    apply_receptor_ligand_atom_coloring,
    apply_scene_atom_coloring,
    set_pymol_scene_context,
)


class MolecularViewerController:
    def __init__(self, window):
        self.w = window
        self.grid_preview_enabled = True

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
        cmd = self.w.pymol_dock.cmd
        path = current_molecule_path(receptor)
        if path is not None and path.exists():
            name = self._receptor_pymol_name(receptor)
            try:
                self.w.pymol_dock.show()
                cmd.delete("all")
                cmd.load(str(path), name)
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
        active_id = int(getattr(receptor, "active_binding_site_id", 0) or 0)
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
        cmd = getattr(self.w.pymol_dock, "cmd", None)
        if cmd is not None:
            try:
                cmd.delete("all")
            except Exception:
                pass

    # -- docking poses -------------------------------------------------------------

    @staticmethod
    def _repair_pose_sdf_if_needed(path: Path) -> None:
        # ponytail: one-time self-heal for SDFs corrupted by the old write_sd_string tuple-repr
        # bug — they're a literal "('...\\n...', [])" instead of raw SDF. Rewrite the real text so
        # existing results render without re-docking. New files are written correctly already.
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if not text.lstrip().startswith(("('", '("')):
            return
        import ast

        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return
        sd = value[0] if isinstance(value, tuple) and value else value
        if isinstance(sd, str) and sd.strip():
            path.write_text(sd, encoding="utf-8")

    @staticmethod
    def _load_pose_state(cmd, pose_path: str, obj: str, pose_rank: int = 1) -> None:
        """Load ONE pose of a multi-pose SDF as a single-state object.

        Loading the file whole gave the object all 9 poses as states and `set state` then
        pinned it to one: the viewer counted 9 states that never changed, for every pose.
        Copying just the selected state out keeps the count honest (1 pose = 1 state) and
        the table stays the only pose selector.
        """
        tmp = f"{obj}__all"
        cmd.load(pose_path, tmp)
        cmd.create(obj, tmp, int(pose_rank), 1)
        cmd.delete(tmp)

    @staticmethod
    def _same_existing_path(left: Path | None, right: Path | None) -> bool:
        if left is None or right is None:
            return False
        try:
            return left.exists() and right.exists() and left.resolve() == right.resolve()
        except Exception:
            return False

    def load_hit(self, hit: DockingHitSummary, pose_rank: int = 1) -> None:
        dock = self.w.pymol_dock
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        if str(getattr(hit, "run_kind", "") or "").strip().lower() == "redocking":
            if self._load_redocking_hit(cmd, dock, hit, pose_rank):
                return
        # The docked POSE lives in output_path (the .dock.sdf); ligand_path is the undocked input.
        pose_path = hit.output_path if hit.output_path is not None and hit.output_path.exists() else None
        if pose_path is None and hit.ligand_path is not None and hit.ligand_path.exists():
            pose_path = hit.ligand_path
        # Results are grouped by receptor, so load it ONCE: only reload when the receptor changes,
        # then just swap the ligand pose. Avoids re-loading the receptor on every ligand/pose click.
        receptor_obj = f"receptor_{hit.receptor_id}"
        ligand_obj = "amdock_result_pose"
        try:
            dock.show()
            # Ask PyMOL what it actually holds instead of remembering what we loaded: switching
            # tabs runs cmd.delete("all") (sync_to_active_view), and a cached "receptor N
            # is already loaded" flag then hides the receptor for the rest of the session.
            try:
                loaded = receptor_obj in set(cmd.get_names("objects"))
            except Exception:
                loaded = False
            if not loaded:
                cmd.delete("all")
                if hit.receptor_path is not None and hit.receptor_path.exists():
                    cmd.load(str(hit.receptor_path), receptor_obj)
                    apply_receptor_atom_coloring(cmd, receptor_obj)
                    cmd.zoom(receptor_obj, 3)
            else:
                cmd.delete(ligand_obj)  # keep the receptor; replace only the pose
            if pose_path is not None:
                self._repair_pose_sdf_if_needed(pose_path)
                self._load_pose_state(cmd, str(pose_path), ligand_obj, pose_rank)
                try:
                    cmd.show("sticks", ligand_obj)
                    apply_ligand_atom_coloring(cmd, ligand_obj)
                    cmd.zoom(ligand_obj, 5)
                except Exception:
                    pass
            selections = {"receptor": receptor_obj}
            if pose_path is not None:
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

    def _load_redocking_hit(self, cmd, dock, hit: DockingHitSummary, pose_rank: int = 1) -> bool:
        pose_path = hit.output_path if hit.output_path is not None and hit.output_path.exists() else None
        reference_ligand_path = (
            hit.reference_ligand_path
            if hit.reference_ligand_path is not None and hit.reference_ligand_path.exists()
            else hit.ligand_path if hit.ligand_path is not None and hit.ligand_path.exists()
            else None
        )
        reference_receptor_path = (
            hit.reference_receptor_path
            if hit.reference_receptor_path is not None and hit.reference_receptor_path.exists()
            else None
        )
        receptor_path = hit.receptor_path if hit.receptor_path is not None and hit.receptor_path.exists() else None
        if pose_path is None and reference_ligand_path is None:
            return False
        try:
            dock.show()
            cmd.delete("all")
            reference_receptor_obj = "redock_reference_receptor"
            docking_receptor_obj = "redock_docking_receptor"
            reference_ligand_obj = "redock_reference_ligand"
            docked_pose_obj = "redock_docked_pose"
            if reference_receptor_path is not None:
                cmd.load(str(reference_receptor_path), reference_receptor_obj)
                apply_receptor_atom_coloring(cmd, reference_receptor_obj)
            if receptor_path is not None and not self._same_existing_path(reference_receptor_path, receptor_path):
                cmd.load(str(receptor_path), docking_receptor_obj)
                apply_receptor_atom_coloring(cmd, docking_receptor_obj)
                try:
                    cmd.align(docking_receptor_obj, reference_receptor_obj)
                except Exception:
                    pass
            elif reference_receptor_path is None and receptor_path is not None:
                cmd.load(str(receptor_path), reference_receptor_obj)
                apply_receptor_atom_coloring(cmd, reference_receptor_obj)
            ligand_objs: list[str] = []
            if reference_ligand_path is not None:
                cmd.load(str(reference_ligand_path), reference_ligand_obj)
                ligand_objs.append(reference_ligand_obj)
            if pose_path is not None:
                self._repair_pose_sdf_if_needed(pose_path)
                self._load_pose_state(cmd, str(pose_path), docked_pose_obj, pose_rank)
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
            receptor_objs: list[str] = []
            if reference_receptor_path is not None or (
                    reference_receptor_path is None and receptor_path is not None
            ):
                receptor_objs.append(reference_receptor_obj)
            if receptor_path is not None and not self._same_existing_path(
                    reference_receptor_path,
                    receptor_path,
            ):
                receptor_objs.append(docking_receptor_obj)
            set_pymol_scene_context(
                dock,
                "redocking",
                target="all",
                selections={
                    "receptor": " or ".join(receptor_objs) or "polymer.protein",
                    "ligand": " or ".join(ligand_objs),
                },
                default_preset="amdockvs.complex",
            )
            return True
        except Exception:
            return False

    # -- shown from the Details panel ----------------------------------------------

    def show_file(self, path_text: str, object_name: str) -> None:
        path = Path(str(path_text or "")).expanduser()
        if not path.exists():
            return
        dock = self.w.pymol_dock
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(path), str(object_name or "selected_file"))
            apply_scene_atom_coloring(self.w)
            cmd.zoom("all", 3)
            loaded_object = str(object_name or "selected_file")
            set_pymol_scene_context(
                dock,
                "generic",
                target=loaded_object,
                selections={"molecule": loaded_object},
            )
        except Exception:
            return

    def show_molecule(self, molecule, mode: str) -> None:
        path = current_molecule_path(molecule) if str(
            mode or "").strip().lower() == "current" else stored_molecule_path(molecule)
        if path is None or not path.exists():
            return
        dock = self.w.pymol_dock
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        object_name = f"molecule_{getattr(molecule, 'id', 'selected')}"
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(path), object_name)
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
            try:
                cmd.orient(object_name)
            except Exception:
                pass
            set_pymol_scene_context(
                dock,
                context_kind,
                target=object_name,
                selections={context_role: object_name},
                default_preset=default_preset,
            )
        except Exception:
            return

    def show_binding_site(self, molecule, site) -> None:
        receptor_path = current_molecule_path(molecule)
        if receptor_path is None or not receptor_path.exists():
            return
        dock = self.w.pymol_dock
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        receptor_name = f"receptor_{getattr(molecule, 'id', 'selected')}"
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(receptor_path), receptor_name)
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

    def show_complex(self, pair) -> None:
        dock = self.w.pymol_dock
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        reference_path = Path(str(getattr(pair, "reference_receptor_path", "") or "").strip()).expanduser()
        if not reference_path.is_absolute():
            project_root = get_default_project_root()
            if project_root is not None:
                reference_path = (project_root / reference_path).resolve()
        ligand = self.w.runtime.molecules.get(
            int(getattr(pair, "ligand_molecule_id", 0) or 0)
        )
        receptor = self.w.runtime.molecules.get(
            int(getattr(pair, "receptor_molecule_id", 0) or 0)
        )
        ligand_path = stored_molecule_path(ligand) if ligand is not None else None
        if not reference_path.exists():
            return
        try:
            dock.show()
            cmd.delete("all")
            receptor_obj = f"complex_receptor_{int(getattr(pair, 'id', 0) or 0)}"
            ligand_obj = f"complex_ligand_{int(getattr(pair, 'id', 0) or 0)}"
            cmd.load(str(reference_path), receptor_obj)
            if ligand_path is not None and ligand_path.exists():
                cmd.load(str(ligand_path), ligand_obj)
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
            if ligand_path is not None and ligand_path.exists():
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
