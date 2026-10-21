"""Ligand-centric off-target / selectivity view.

The default Docking Results view is receptor -> ligand -> pose. This one flips it:
rows are ligands, columns are receptors. The Selector matrix shows each ligand's
value (affinity or ligand efficiency) per receptor — with a pose combobox on the
reference column — and the Difference matrix shows reference - receptor, i.e. how
much more/less the ligand prefers the reference target over each off-target.

ponytail: off-target analysis is a shortlist exercise, so we cap to the top
LIGAND_CAP ligands of the reference receptor and to RECEPTOR_CAP (<=10) receptors.
The pose combobox lives only on the reference column (the pose you inspect drives
the difference); off-target columns use each receptor's best pose.
"""
from __future__ import annotations

import logging
from math import isfinite

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pyqtgraph import ColorMap

from amdockvs.core.payloads import payload_store
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.tools.pymol_ribbon import (
    apply_ligand_atom_coloring,
    apply_receptor_atom_coloring,
    fetch_structures,
    load_structure,
    replace_comparison_structure,
    set_pymol_scene_context,
)
from amdockvs.ui.view_ids import OFFTARGET_VIEW_ID

_log = logging.getLogger(__name__)

LIGAND_CAP = 200
RECEPTOR_CAP = 10

_METRICS = (("Affinity (kcal/mol)", "score"), ("Ligand efficiency", "ligand_efficiency"))
_DIFFERENCE_PALETTES = (
    ("Blue – white – red", ("#2166ac", "#ffffff", "#b2182b")),
    ("Purple – white – orange", ("#762a83", "#ffffff", "#e66101")),
)


class OffTargetResultsWidget(QWidget):
    data_refreshed = Signal(bool)

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._receptors: list[tuple[int, str]] = []  # (id, name), best-score first
        self._columns: list[tuple[int, str]] = []     # reference first, then off-targets
        self._row_state: dict[int, dict] = {}
        self._grid_ligand_id: int | None = None
        self._grid_request = 0
        self._token = 0
        self._data_signature = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 5, 0, 5)
        controls = QHBoxLayout()
        self.metric_combo = QComboBox(self)
        for label, key in _METRICS:
            self.metric_combo.addItem(label, key)
        self.metric_combo.currentIndexChanged.connect(self._load_matrix)
        self.reference_combo = QComboBox(self)
        self.reference_combo.currentIndexChanged.connect(self._load_matrix)
        controls.addWidget(QLabel("Metric:", self))
        controls.addWidget(self.metric_combo)
        controls.addWidget(QLabel("Reference receptor:", self))
        controls.addWidget(self.reference_combo, 1)
        self.align_check = QCheckBox("Align receptors", self)
        self.align_check.setChecked(True)
        self.align_check.setToolTip(
            "When showing a ligand's grid, superimpose every receptor onto the reference "
            "(and move its bound pose with it) so the binding modes are directly comparable — "
            "no per-ligand matrices needed (AMDock v1 style)."
        )
        controls.addWidget(self.align_check)
        outer.addLayout(controls)

        colors = QHBoxLayout()
        colors.addWidget(QLabel("Difference colors:", self))
        self.palette_combo = QComboBox(self)
        for label, stops in _DIFFERENCE_PALETTES:
            self.palette_combo.addItem(label, stops)
        colors.addWidget(self.palette_combo)
        legend = QVBoxLayout()
        self.color_scale = QLabel(self)
        self.color_scale.setAlignment(Qt.AlignCenter)
        self.color_scale.setMinimumWidth(280)
        self.color_scale.setToolTip(
            "Difference = reference − off-target. Negative values favor the reference. "
            "Colors saturate below −5 and above +5; values keep their actual magnitude."
        )
        self.color_bar = QLabel(self)
        self.color_bar.setFixedHeight(10)
        legend.addWidget(self.color_bar)
        legend.addWidget(self.color_scale)
        colors.addLayout(legend)
        colors.addStretch()
        outer.addLayout(colors)

        self.note = QLabel("Click a ligand's name to show all receptors + poses in a PyMOL grid.", self)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)

        splitter = QSplitter(Qt.Horizontal, self)
        self.selector_table = self._make_table()
        self.selector_table.cellClicked.connect(self._on_selector_clicked)
        self.difference_table = self._make_table()
        splitter.addWidget(self._titled("Selector Matrix", self.selector_table))
        splitter.addWidget(self._titled("Difference Matrix", self.difference_table))
        splitter.setSizes([700, 500])
        outer.addWidget(splitter, 1)

        self.palette_combo.currentIndexChanged.connect(self._update_difference_palette)
        self.metric_combo.currentIndexChanged.connect(self._update_difference_palette)
        self._update_difference_palette()
        self.refresh()

    # --- construction helpers -------------------------------------------------
    @staticmethod
    def _make_table() -> QTableWidget:
        table = QTableWidget(0, 0)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)
        return table

    @staticmethod
    def _titled(title: str, table: QTableWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title, box))
        layout.addWidget(table, 1)
        return box

    # --- data flow ------------------------------------------------------------
    def refresh(self) -> None:
        # Only show the blocking "Loading…" overlay on the very first populate — never on a
        # silent reload over a matrix you're already reading.
        run_async(
            self.runtime.docking.receptor_summaries,
            self._apply_receptors,
            on_error=lambda _exc: self._apply_receptors([]),
            busy=self if not self._receptors else None,
        )

    def refresh_view(self) -> None:
        # The framework polls this on a timer while jobs run. Rebuilding a populated matrix would
        # reset the user's pose selections + scroll mid-analysis (and flash the overlay), and rows
        # past the viewport aren't visible anyway — so only auto-populate when the matrix is empty.
        if self.selector_table.rowCount() == 0:
            self.refresh()

    def _apply_receptors(self, summaries) -> None:
        self._receptors = [(int(s.receptor_id), s.receptor_name or f"#{s.receptor_id}") for s in summaries]
        previous = self.reference_combo.currentData()
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        for rid, name in self._receptors:
            self.reference_combo.addItem(name, rid)
        if previous is not None:
            idx = self.reference_combo.findData(previous)
            if idx >= 0:
                self.reference_combo.setCurrentIndex(idx)
        self.reference_combo.blockSignals(False)
        if not self._receptors:
            self.note.setText("No docking results yet.")
            self.selector_table.setRowCount(0)
            self.difference_table.setRowCount(0)
            signature = ()
            changed = signature != self._data_signature
            self._data_signature = signature
            self.data_refreshed.emit(changed)
            return
        self._load_matrix()

    def _load_matrix(self) -> None:
        if not self._receptors:
            return
        ref_id = self.reference_combo.currentData()
        if ref_id is None:
            ref_id = self._receptors[0][0]
        ref_id = int(ref_id)
        others = [r for r in self._receptors if r[0] != ref_id][: RECEPTOR_CAP - 1]
        self._columns = [next(r for r in self._receptors if r[0] == ref_id), *others]
        column_ids = [rid for rid, _ in self._columns]
        self._token += 1
        token = self._token

        def _fetch():
            hits = self.runtime.docking.top_hits(receptor_id=ref_id, limit=LIGAND_CAP)
            ligand_ids, names = [], {}
            for hit in hits:
                if hit.ligand_id not in names:
                    ligand_ids.append(hit.ligand_id)
                    names[hit.ligand_id] = hit.ligand_name
            rows = self.runtime.docking.offtarget_rows(receptor_ids=column_ids, ligand_ids=ligand_ids)
            return ligand_ids, names, rows

        # Overlay only when there's nothing on screen yet; a reload (metric/reference change)
        # over an existing matrix happens silently so it doesn't block what you're viewing.
        busy = self if self.selector_table.rowCount() == 0 else None
        run_async(_fetch, lambda payload: self._apply_matrix(payload, token), busy=busy)

    def _apply_matrix(self, payload, token: int) -> None:
        if token != self._token:
            return
        ligand_ids, names, rows = payload
        signature = tuple(sorted(
            (
                int(row.get("result_id") or 0),
                int(row.get("ligand_id") or 0),
                int(row.get("receptor_id") or 0),
                int(row.get("pose_rank") or 0),
                repr(row.get("score")),
                repr(row.get("ligand_efficiency")),
                str(row.get("updated_at") or ""),
            )
            for row in rows
        ))
        changed = signature != self._data_signature
        self._data_signature = signature
        # matrix[(ligand_id, receptor_id)] = poses sorted by rank
        matrix: dict[tuple[int, int], list[dict]] = {}
        for row in rows:
            matrix.setdefault((row["ligand_id"], row["receptor_id"]), []).append(row)
        for poses in matrix.values():
            poses.sort(key=lambda p: p["pose_rank"])

        ref_id = self._columns[0][0]
        others = self._columns[1:]
        truncated = []
        if len(self._receptors) > RECEPTOR_CAP:
            truncated.append(f"showing {RECEPTOR_CAP} of {len(self._receptors)} receptors")
        if len(ligand_ids) >= LIGAND_CAP:
            truncated.append(f"top {LIGAND_CAP} ligands of the reference")
        self.note.setText(" · ".join(truncated))

        self.selector_table.clear()
        self.difference_table.clear()
        self.selector_table.setColumnCount(1 + len(self._columns))
        self.selector_table.setHorizontalHeaderLabels(
            ["Ligand", *[f"{n} (R)" if rid == ref_id else n for rid, n in self._columns]]
        )
        self.difference_table.setColumnCount(1 + len(others))
        self.difference_table.setHorizontalHeaderLabels(["Ligand", *[n for _, n in others]])

        visible = [lid for lid in ligand_ids if matrix.get((lid, ref_id))]
        self.selector_table.setRowCount(len(visible))
        self.difference_table.setRowCount(len(visible))
        self._row_state.clear()
        for r, lid in enumerate(visible):
            self._row_state[r] = {
                "ligand_id": lid,
                "ligand_name": names.get(lid, str(lid)),
                # every receptor column keeps ALL its poses so each has its own pose selector
                "poses_by_receptor": {rid: (matrix.get((lid, rid)) or []) for rid, _ in self._columns},
            }
            self._render_row(r)
        self.data_refreshed.emit(changed)

    # The parent Results view uses this method for live and terminal-job refreshes. Unlike
    # refresh_view(), it intentionally accepts new rows while a screening is still running.
    refresh_results_view = refresh

    # --- rendering ------------------------------------------------------------
    def _metric_key(self) -> str:
        return self.metric_combo.currentData() or "score"

    def _value(self, pose: dict | None) -> float | None:
        if pose is None:
            return None
        v = pose.get(self._metric_key())
        return None if v is None else float(v)

    @staticmethod
    def _fmt(value: float | None) -> str:
        return "—" if value is None else f"{value:.2f}"

    def _render_row(self, r: int) -> None:
        state = self._row_state[r]
        lig_item = QTableWidgetItem(state["ligand_name"])
        lig_item.setData(Qt.UserRole, state["ligand_id"])
        self.selector_table.setItem(r, 0, lig_item)
        self.difference_table.setItem(r, 0, QTableWidgetItem(state["ligand_name"]))

        # every receptor column gets its own pose selector (not just the reference)
        for col, (rid, _name) in enumerate(self._columns, start=1):
            poses = state["poses_by_receptor"].get(rid) or []
            if not poses:
                self.selector_table.setCellWidget(r, col, None)
                self.selector_table.setItem(r, col, QTableWidgetItem("—"))
                continue
            combo = QComboBox(self.selector_table)
            for pose in poses:
                combo.addItem(f"P{pose['pose_rank']}: {self._fmt(self._value(pose))}", pose["pose_rank"])
            combo.currentIndexChanged.connect(lambda _i, row=r, receptor=rid: self._on_pose_changed(row, receptor))
            self.selector_table.setCellWidget(r, col, combo)
        self._render_difference_row(r)

    def _column_index(self, receptor_id: int) -> int | None:
        return next((i for i, (rid, _) in enumerate(self._columns, start=1) if rid == receptor_id), None)

    def _selected_pose(self, r: int, receptor_id: int) -> dict | None:
        poses = self._row_state[r]["poses_by_receptor"].get(receptor_id) or []
        if not poses:
            return None
        col = self._column_index(receptor_id)
        combo = self.selector_table.cellWidget(r, col) if col is not None else None
        if isinstance(combo, QComboBox):
            idx = max(0, combo.currentIndex())
            if idx < len(poses):
                return poses[idx]
        return poses[0]

    def _update_difference_palette(self) -> None:
        stops = self.palette_combo.currentData()
        self._difference_colormap = ColorMap([0.0, 0.5, 1.0], [QColor(c) for c in stops])
        unit = "kcal/mol" if self._metric_key() == "score" else "kcal/mol/heavy atom"
        self.color_scale.setText(f"−5                 0                 +5 {unit}")
        self.color_bar.setStyleSheet(
            "QLabel { background: "
            f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {stops[0]}, "
            f"stop:0.5 {stops[1]}, stop:1 {stops[2]}); }}"
        )
        for r in self._row_state:
            self._render_difference_row(r)

    def _render_difference_row(self, r: int) -> None:
        ref_val = self._value(self._selected_pose(r, self._columns[0][0]))
        for col, (rid, _name) in enumerate(self._columns[1:], start=1):
            rec_val = self._value(self._selected_pose(r, rid))
            diff = None if ref_val is None or rec_val is None else ref_val - rec_val
            if diff is None or not isfinite(diff):
                item = QTableWidgetItem("—")
            else:
                item = QTableWidgetItem(f"{diff:+.2f}")
                background = self._difference_colormap.mapToQColor(max(0.0, min(1.0, (diff + 5) / 10)))
                item.setBackground(background)
                channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                            for c in background.getRgbF()[:3]]
                luminance = sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))
                item.setForeground(QColor("black" if luminance > 0.179 else "white"))
            self.difference_table.setItem(r, col, item)

    # --- interaction ----------------------------------------------------------
    def hideEvent(self, event) -> None:
        if not event.spontaneous():
            self._grid_request += 1
            cmd, dock = self._pymol()
            context = getattr(dock, "scene_context", None)
            if self._grid_ligand_id is not None and context is not None and context.is_comparison:
                dock.set_scene_context("generic")
            self._grid_ligand_id = None
        super().hideEvent(event)

    def _on_pose_changed(self, r: int, receptor_id: int) -> None:
        self._render_difference_row(r)
        pose = self._selected_pose(r, receptor_id)
        cmd, dock = self._pymol()
        if pose is None or cmd is None:
            return
        context = getattr(dock, "scene_context", None)
        objects = cmd.get_names("objects")
        if (context is not None and context.is_comparison
                and self._grid_ligand_id == self._row_state[r]["ligand_id"]
                and f"rec_{receptor_id}" in objects and f"lig_{receptor_id}" in objects):
            self._replace_grid_pose(dock, r, receptor_id, pose)
        else:
            self._show_ligand_grid(r)

    def _on_selector_clicked(self, row: int, col: int) -> None:
        # Clicking the ligand name shows every receptor + its selected pose in a PyMOL grid.
        if col == 0:
            self._show_ligand_grid(row)

    def _pymol(self):
        dock = getattr(self.window(), "pymol_dock", None)
        return getattr(dock, "cmd", None) if dock is not None else None, dock

    def _show_ligand_grid(self, r: int) -> None:
        """Tile every receptor (with its selected pose) for this ligand in a PyMOL grid.
        With 'Align receptors' on, each receptor is superimposed onto the reference and its
        pose is carried along by the same matrix, so binding modes are directly comparable."""
        cmd, dock = self._pymol()
        if cmd is None:
            return
        ref_id = self._columns[0][0]
        wanted = {}
        complex_names = {}
        ligand_id = self._row_state[r]["ligand_id"]
        ligand_name = self._row_state[r]["ligand_name"]
        for rid, receptor_name in self._columns:
            pose = self._selected_pose(r, rid)
            if pose is not None and pose.get("receptor_payload_id") and pose.get("pose_payload_id"):
                wanted[f"rec_{rid}"] = int(pose["receptor_payload_id"])
                wanted[f"lig_{rid}"] = int(pose["pose_payload_id"])
                complex_names[rid] = f"{receptor_name}-{ligand_name}[P{pose.get('pose_rank') or 1}]"
        store = payload_store(self.runtime)
        self._grid_request += 1
        request = self._grid_request

        def draw(found: dict) -> None:
            if request == self._grid_request:
                self._draw_ligand_grid(cmd, dock, ref_id, found, complex_names, ligand_id)

        run_async(
            lambda: fetch_structures(store, wanted) if wanted else {},
            draw,
            lambda error: _log.warning("Could not read the poses for the PyMOL grid: %s", error),
        )

    def _draw_ligand_grid(
        self, cmd, dock, ref_id: int, found: dict, complex_names: dict[int, str], ligand_id: int,
    ) -> None:
        try:
            dock.show()
            cmd.delete("all")
            ref_receptor_obj = None
            loaded_receptor_objs: list[str] = []
            loaded_ligand_objs: list[str] = []
            grid_labels = {}
            slot = 0
            for rid, complex_name in complex_names.items():
                rec_obj, lig_obj = f"rec_{rid}", f"lig_{rid}"
                if rec_obj not in found or lig_obj not in found:
                    continue
                slot += 1
                grid_labels[slot] = complex_name
                load_structure(cmd, found[rec_obj], rec_obj)
                load_structure(cmd, found[lig_obj], lig_obj)
                loaded_receptor_objs.append(rec_obj)
                loaded_ligand_objs.append(lig_obj)
                apply_receptor_atom_coloring(cmd, rec_obj)
                try:
                    cmd.show("sticks", lig_obj)
                    apply_ligand_atom_coloring(cmd, lig_obj, slot - 1)
                except Exception:
                    pass
                # receptor + its pose share one grid slot so they tile together
                cmd.set("grid_slot", slot, rec_obj)
                cmd.set("grid_slot", slot, lig_obj)
                if rid == ref_id:
                    ref_receptor_obj = rec_obj
            if self.align_check.isChecked() and ref_receptor_obj is not None:
                for rid in complex_names:
                    if rid == ref_id:
                        continue
                    rec_obj, lig_obj = f"rec_{rid}", f"lig_{rid}"
                    try:
                        cmd.align(rec_obj, ref_receptor_obj)
                        cmd.matrix_copy(rec_obj, lig_obj)  # move the pose with its receptor
                    except Exception:
                        pass
            cmd.zoom("all")
            set_pymol_scene_context(
                dock,
                "comparison",
                target="all",
                selections={
                    "receptor": " or ".join(loaded_receptor_objs),
                    "ligand": " or ".join(loaded_ligand_objs),
                },
                default_preset="amdockvs.complex",
                grid_labels=grid_labels,
            )
            self._grid_ligand_id = ligand_id
        except Exception:
            return

    def _replace_grid_pose(self, dock, r: int, receptor_id: int, pose: dict) -> None:
        payload_id = pose.get("pose_payload_id")
        if not payload_id:
            return
        ligand_id = self._row_state[r]["ligand_id"]
        receptor_name = dict(self._columns)[receptor_id]
        title = f"{receptor_name}-{self._row_state[r]['ligand_name']}[P{pose.get('pose_rank') or 1}]"
        rec_obj, lig_obj = f"rec_{receptor_id}", f"lig_{receptor_id}"
        store = payload_store(self.runtime)

        def draw(found: dict) -> None:
            if self._grid_ligand_id != ligand_id or self._row_state.get(r, {}).get("ligand_id") != ligand_id:
                return
            selected = self._selected_pose(r, receptor_id)
            context = dock.scene_context
            if (selected is None or selected.get("pose_payload_id") != payload_id
                    or selected.get("pose_rank") != pose.get("pose_rank")
                    or not context.is_comparison):
                return
            if lig_obj in found:
                replace_comparison_structure(dock, found[lig_obj], lig_obj, reference=rec_obj, label=title)

        run_async(
            lambda: fetch_structures(store, {lig_obj: int(payload_id)}),
            draw,
            lambda error: _log.warning("Could not read the replacement pose for the PyMOL grid: %s", error),
        )

def register_offtarget_workspace(window) -> None:
    window.register_main_view(
        OFFTARGET_VIEW_ID,
        "Off-target",
        lambda: OffTargetResultsWidget(
            runtime=window.runtime,
            parent=window.central_widget,
        ),
    )


__all__ = ["OffTargetResultsWidget", "register_offtarget_workspace"]
