"""Ligand-centric off-target / selectivity view.

The default Docking Results view is receptor -> ligand -> pose. This one flips it:
rows are ligands, columns are receptors, and one matrix holds both what you pick and
what it means. The target column shows the affinity of its selected pose; every
off-target cell shows the selectivity of the ligand between that receptor and the
target (ΔΔG either way round, or fold in Ki), blue where the ligand prefers the
target and red where it prefers the off-target, with the pose it uses and that pose's
own affinity.

Only the affinity is compared. LE, pKi, LLE, BEI, SEI and fit quality are the score
times a constant of the ligand (ΔLLE is ΔpKi exactly), so between receptors of one
ligand they rank exactly like it and would only relabel the same number.

Rows are every ligand the target scored, paged by the SmartTable on demand, so the
matrix has no ligand cap; each off-target column is a SQL subquery at its selected pose.
ponytail: RECEPTOR_CAP (<=10) receptors — one correlated lookup per column per row on screen.
"""
from __future__ import annotations

import logging
from math import isfinite

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from pyqtgraph import ColorMap

from ms_components.chips import FlowLayout
from ms_components.ms_table.table_model import COLUMN_DEF_ROLE
from ms_components.theme import color as theme_color

from amdockvs.core.payloads import payload_store
from amdockvs.docking.results.metrics import predicted_ki_m
from amdockvs.docking.results.tables import offtarget_config
from amdockvs.ui.catalog.common import project_table
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

RECEPTOR_CAP = 10
# A difference smaller than the docking error (~2 kcal/mol) says little, so the colours
# saturate only at a gap that tells two receptors apart; the spin box retunes it.
DEFAULT_SCALE = 4.0

_SHOW = (
    ("ΔΔG off-target − target", "off-minus-target"),
    ("ΔΔG target − off-target", "target-minus-off"),
    ("Fold (Ki off-target / Ki target)", "fold"),
)
_HINTS = {
    "off-minus-target": "ΔΔG = off-target − target (kcal/mol). A more negative affinity binds better, "
                        "so a positive ΔΔG means the ligand prefers the target.",
    "target-minus-off": "ΔΔG = target − off-target (kcal/mol). A more negative affinity binds better, "
                        "so a negative ΔΔG means the ligand prefers the target.",
    "fold": "Fold = Ki(off-target) / Ki(target), from the affinities. ×10 means the target binds "
            "10 times tighter; below ×1 the off-target binds tighter. Docking scores carry ~2 kcal/mol "
            "of error, about ×30.",
}
_DIFFERENCE_PALETTES = (
    ("Blue – white – red", ("#2166ac", "#ffffff", "#b2182b")),
    ("Purple – white – orange", ("#762a83", "#ffffff", "#e66101")),
)


def _format_fold(fold: float) -> str:
    return f"×{fold:.2g}" if fold < 10 else f"×{fold:,.0f}"


def _finite(value) -> float | None:
    return None if value is None or not isfinite(float(value)) else float(value)


def _contrast(background: QColor) -> QColor:
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in background.getRgbF()[:3]]
    luminance = sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))
    return QColor("black" if luminance > 0.179 else "white")


def _receptor_of(field: str, target_id: int) -> int | None:
    """The receptor a matrix column shows: `target_score` is the target, `ddg_<id>` an off-target."""
    if field == "target_score":
        return target_id
    return int(field[4:]) if field.startswith("ddg_") else None


class _CellDelegate(QStyledItemDelegate):
    """Pose chip top-left, value centred, the pose's own affinity small bottom-right.

    What a cell shows comes from the widget (`cell(row_data, receptor_id)`), not the model: the
    pose picked for one cell and the Show/colour settings live there, and the rows stay SQL."""

    def __init__(self, widget: "OffTargetResultsWidget", parent=None):
        super().__init__(parent)
        self._widget = widget

    def paint(self, painter, option, index):
        col_def = index.data(COLUMN_DEF_ROLE)
        row_data = index.model().get_row_data(index.row())
        rid = None if col_def is None or not self._widget._columns else _receptor_of(
            col_def.field, self._widget._columns[0][0])
        if rid is None or row_data is None:
            return super().paint(painter, option, index)
        text, rank, detail, background = self._widget.cell(row_data, rid)
        rect = option.rect.adjusted(1, 1, -1, -1)
        palette = option.palette
        painter.save()
        painter.fillRect(rect, background if background is not None else palette.color(QPalette.AlternateBase))
        color = _contrast(background) if background is not None else palette.color(QPalette.Text)
        dim = QColor(color)
        dim.setAlphaF(0.6)
        if option.state & QStyle.State_Selected:
            # a frame, not a fill: the heatmap stays readable on the selected row
            painter.setPen(QPen(palette.color(QPalette.Highlight), 2))
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.8)
        if rank is not None:
            chip = QRect(rect.left() + 3, rect.top() + 2, 22, 13)
            if rank != 1:  # not the best pose
                small.setBold(True)
                painter.setPen(QPen(theme_color("orange"), 1.2))
                painter.drawRoundedRect(chip, 3, 3)
                painter.setPen(color)
            else:
                painter.setPen(dim)
            painter.setFont(small)
            painter.drawText(chip, Qt.AlignCenter, f"P{rank}")
        big = QFont(option.font)
        big.setPointSizeF(option.font.pointSizeF() * 1.1)
        big.setBold(True)
        painter.setFont(big)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignCenter, text)
        if detail:
            small.setBold(False)
            painter.setFont(small)
            painter.setPen(dim)
            painter.drawText(rect.adjusted(0, 0, -4, -2), Qt.AlignRight | Qt.AlignBottom, detail)
        painter.restore()


class OffTargetResultsWidget(QWidget):
    data_refreshed = Signal(bool)

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._receptors: list[tuple[int, str]] = []  # (id, name), best-score first
        self._columns: list[tuple[int, str]] = []     # target first, then off-targets
        self._protocol = ""
        self._ranks: dict[int, int] = {}              # receptor -> the pose its column shows
        self._overrides: dict[tuple[int, int], int] = {}  # (ligand, receptor) -> pose picked in a cell
        self._poses: dict[int, dict[int, list[dict]]] = {}  # ligand -> receptor -> its poses (on demand)
        self._names: dict[int, str] = {}
        self._grid_ligand_id: int | None = None
        self._grid_request = 0
        self._token = 0
        self._signature = None
        self.table = None

        outer = self._outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 5, 0, 5)
        self.reference_combo = QComboBox(self)
        self.reference_combo.currentIndexChanged.connect(self._load_target)
        self.protocol_combo = QComboBox(self)
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        self.show_combo = QComboBox(self)
        for label, key in _SHOW:
            self.show_combo.addItem(label, key)
        self.align_check = QCheckBox("Align receptors", self)
        self.align_check.setChecked(True)
        self.align_check.setToolTip(
            "When showing a ligand's grid, superimpose every receptor onto the target "
            "(and move its bound pose with it) so the binding modes are directly comparable — "
            "no per-ligand matrices needed (AMDock v1 style)."
        )
        self.palette_combo = QComboBox(self)
        for label, stops in _DIFFERENCE_PALETTES:
            self.palette_combo.addItem(label, stops)
        self.scale_spin = QDoubleSpinBox(self)
        self.scale_spin.setRange(0.5, 10.0)
        self.scale_spin.setSingleStep(0.5)
        self.scale_spin.setDecimals(1)
        self.scale_spin.setPrefix("± ")
        self.scale_spin.setSuffix(" kcal/mol")
        self.scale_spin.setValue(DEFAULT_SCALE)
        self.scale_spin.setToolTip("ΔΔG at which the colours saturate; values keep their actual magnitude.")
        self.color_bar = QLabel(self)
        self.color_bar.setFixedHeight(10)
        self.color_scale = QLabel(self)
        self.color_scale.setAlignment(Qt.AlignCenter)
        self.pose_combo = QComboBox(self)
        self.pose_column_combo = QComboBox(self)
        apply_pose = QPushButton("Apply", self)
        apply_pose.setToolTip(
            "Show this pose in the chosen column(s). A ligand without that pose shows —; "
            "the list of ligands never changes."
        )
        apply_pose.clicked.connect(self._apply_pose)

        # The groups wrap onto new lines, so a narrow Results pane stays usable.
        controls = QWidget(self)
        flow = FlowLayout(controls, spacing=6)
        self._protocol_group = self._group(QLabel("Program:", self), self.protocol_combo)
        self._protocol_group.setVisible(False)
        for group in (
            self._group(QLabel("Target receptor:", self), self.reference_combo),
            self._protocol_group,
            self._group(QLabel("Show:", self), self.show_combo),
            self._group(self.align_check),
            self._group(QLabel("Colors:", self), self.palette_combo, self.scale_spin),
            self._group(self.color_bar, self.color_scale, vertical=True),
            self._group(QLabel("Pose:", self), self.pose_combo, QLabel("in", self), self.pose_column_combo, apply_pose),
        ):
            flow.addWidget(group)
        outer.addWidget(controls)

        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(placeholder-text);")
        outer.addWidget(self.hint)
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        outer.addStretch(1)

        self.palette_combo.currentIndexChanged.connect(self._render_all)
        self.show_combo.currentIndexChanged.connect(self._render_all)
        self.scale_spin.valueChanged.connect(self._render_all)
        self._render_all()
        self.refresh()

    def _group(self, *widgets, vertical: bool = False) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box) if vertical else QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0 if vertical else 4)
        for widget in widgets:
            layout.addWidget(widget)
        return box

    # --- data flow ------------------------------------------------------------
    def refresh(self) -> None:
        run_async(
            self.runtime.docking.receptor_summaries,
            self._apply_receptors,
            on_error=lambda _exc: self._apply_receptors([]),
            busy=self if not self._receptors else None,
        )

    # The parent Results view uses this method for live and terminal-job refreshes; it accepts
    # new rows while a screening is still running.
    refresh_results_view = refresh

    def refresh_view(self) -> None:
        # Polled while jobs run: a populated matrix keeps its scroll and pose choices.
        if self.table is None:
            self.refresh()

    def refresh_counts(self) -> bool:
        return self.table.refresh_counts() if self.table is not None else False

    def ensure_viewport_filled(self, force: bool = False) -> bool:
        return self.table.ensure_viewport_filled(force=force) if self.table is not None else True

    def _apply_receptors(self, summaries) -> None:
        self._receptors = [(int(s.receptor_id), s.receptor_name or f"#{s.receptor_id}") for s in summaries]
        previous = self.reference_combo.currentData()
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        for rid, name in self._receptors:
            self.reference_combo.addItem(name, rid)
        index = self.reference_combo.findData(previous) if previous is not None else -1
        self.reference_combo.setCurrentIndex(max(index, 0))
        self.reference_combo.blockSignals(False)
        if not self._receptors:
            self._set_table(None)
            self.note.setText("No docking results yet.")
            self.note.setVisible(True)
            self._emit_refreshed()
            return
        if self.table is not None and self._columns == self._matrix_columns():
            # Same matrix, newer rows (a job is writing): reload in place.
            self.table.refresh_preserving_view()
            self._emit_refreshed()
            return
        self._load_target()

    def _matrix_columns(self) -> list[tuple[int, str]]:
        target_id = self.reference_combo.currentData()
        target = next((r for r in self._receptors if r[0] == target_id), self._receptors[0])
        return [target, *[r for r in self._receptors if r[0] != target[0]][: RECEPTOR_CAP - 1]]

    def _load_target(self, *_args) -> None:
        if not self._receptors:
            return
        columns = self._matrix_columns()
        self._token += 1
        token = self._token
        docking = self.runtime.docking

        def _fetch():
            protocols = docking.result_protocols(receptor_id=columns[0][0])
            return protocols, docking.max_pose_rank(receptor_ids=[rid for rid, _ in columns])

        def _apply(payload):
            if token != self._token:
                return
            protocols, max_rank = payload
            self._columns = columns
            self._ranks, self._overrides, self._poses = {}, {}, {}
            previous = self.protocol_combo.currentData()
            self.protocol_combo.blockSignals(True)
            self.protocol_combo.clear()
            for phash, label in protocols:
                self.protocol_combo.addItem(label, phash)
            index = self.protocol_combo.findData(previous) if previous is not None else -1
            self.protocol_combo.setCurrentIndex(max(index, 0))
            self.protocol_combo.blockSignals(False)
            self._protocol_group.setVisible(len(protocols) > 1)
            self._protocol = str(self.protocol_combo.currentData() or "")
            self.pose_combo.clear()
            for rank in range(1, max(1, int(max_rank)) + 1):
                self.pose_combo.addItem(f"P{rank}", rank)
            self.pose_column_combo.clear()
            self.pose_column_combo.addItem("All receptors", None)
            for i, (rid, name) in enumerate(columns):
                self.pose_column_combo.addItem(f"{name} (T)" if i == 0 else name, rid)
            capped = len(self._receptors) > RECEPTOR_CAP
            self.note.setText(f"showing {RECEPTOR_CAP} of {len(self._receptors)} receptors" if capped else "")
            self.note.setVisible(capped)
            self._rebuild()

        run_async(_fetch, _apply, busy=self if self.table is None else None)

    def _on_protocol_changed(self, _index: int) -> None:
        self._protocol = str(self.protocol_combo.currentData() or "")
        self._overrides, self._poses = {}, {}
        self._rebuild()

    def _rebuild(self, *, keep_scroll: bool = False) -> None:
        """A new table for the current target, protocol and column poses (they are the SQL)."""
        if not self._columns:
            return
        config = offtarget_config(
            target=self._columns[0], off_targets=self._columns[1:], ranks=self._ranks,
            protocol_hash=self._protocol, tooltip=self._tooltip,
        )
        scroll = self.table.view.verticalScrollBar().value() if keep_scroll and self.table is not None else 0
        table = project_table(self.runtime, config, self)
        table.view.horizontalHeader().setStretchLastSection(False)
        table.view.setItemDelegate(_CellDelegate(self, table.view))
        table.view.clicked.connect(self._on_cell_clicked)
        self._set_table(table)
        table.view.verticalScrollBar().setValue(scroll)
        self._emit_refreshed()

    def _set_table(self, table) -> None:
        old, self.table = self.table, table
        if old is not None:
            old.hide()
            old.deleteLater()
        if table is not None:
            self._outer.insertWidget(self._outer.count() - 1, table, 1)
        self._outer.setStretch(self._outer.count() - 1, 0 if table is not None else 1)

    def _emit_refreshed(self) -> None:
        total = 0 if self.table is None else self.table.record_total
        signature = (tuple(self._columns), self._protocol, tuple(sorted(self._ranks.items())), total)
        changed = signature != self._signature
        self._signature = signature
        self.data_refreshed.emit(changed)

    # --- what a cell shows -----------------------------------------------------
    def _rank(self, ligand_id: int, receptor_id: int) -> int:
        return self._overrides.get((ligand_id, receptor_id), self._ranks.get(receptor_id, 1))

    def _pose_score(self, row_data: dict, receptor_id: int) -> tuple[int, float | None]:
        """(pose rank, score) of one cell: its own pick if it has one, else its column's pose."""
        ligand_id = int(row_data["__raw__"].ligand_molecule_id)
        rank = self._rank(ligand_id, receptor_id)
        if (ligand_id, receptor_id) in self._overrides:
            pose = next((p for p in self._poses.get(ligand_id, {}).get(receptor_id, ()) if p["pose_rank"] == rank), None)
            return rank, None if pose is None else _finite(pose["score"])
        target_score = _finite(row_data.get("target_score"))
        if receptor_id == self._columns[0][0]:
            return rank, target_score
        ddg = row_data.get(f"ddg_{receptor_id}")
        return rank, None if ddg is None or target_score is None else _finite(ddg + target_score)

    def cell(self, row_data: dict, receptor_id: int) -> tuple[str, int | None, str, QColor | None]:
        """(value, pose rank, detail, background) of one cell."""
        rank, score = self._pose_score(row_data, receptor_id)
        if score is None:
            return "—", None, "", None
        if receptor_id == self._columns[0][0]:
            return f"{score:.2f}", rank, "", None
        _, ref_score = self._pose_score(row_data, self._columns[0][0])
        if ref_score is None:
            return "—", rank, f"{score:.2f}", None
        prefers_target = score - ref_score  # ΔΔG off-target − target
        mode = self.show_combo.currentData()
        if mode == "fold":
            fold = predicted_ki_m(prefers_target)
            text = "—" if fold is None else _format_fold(fold)
        else:
            text = f"{prefers_target if mode == 'off-minus-target' else -prefers_target:+.2f}"
        scale = self.scale_spin.value()
        background = self._colormap.mapToQColor(
            max(0.0, min(1.0, (scale - prefers_target) / (2 * scale)))  # blue = target
        )
        return text, rank, f"{score:.2f}", background

    def _tooltip(self, receptor_id: int, row_data: dict) -> str:
        names = dict(self._columns)
        target_id = self._columns[0][0]
        rank, score = self._pose_score(row_data, receptor_id)
        if score is None:
            return f"{names[receptor_id]} · P{rank}   no pose"
        tip = f"{names[receptor_id]} · P{rank}   {score:.2f} kcal/mol"
        ref_rank, ref_score = self._pose_score(row_data, target_id)
        if receptor_id != target_id and ref_score is not None:
            tip += (f"\n{names[target_id]} (T) · P{ref_rank}   {ref_score:.2f} kcal/mol"
                    f"\n{self.show_combo.currentText()}   {self.cell(row_data, receptor_id)[0]}")
        return tip

    def _render_all(self) -> None:
        stops = self.palette_combo.currentData()
        self._colormap = ColorMap([0.0, 0.5, 1.0], [QColor(c) for c in stops])
        self.color_scale.setText("prefers target   ·   0   ·   prefers off-target")
        self.color_bar.setStyleSheet(
            "QLabel { background: "
            f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {stops[0]}, "
            f"stop:0.5 {stops[1]}, stop:1 {stops[2]}); }}"
        )
        self.hint.setText(
            _HINTS[self.show_combo.currentData()]
            + " Click a cell to pick its pose; click a ligand's name for the PyMOL grid."
        )
        if self.table is not None:
            self.table.view.viewport().update()

    # --- poses ----------------------------------------------------------------
    def _with_poses(self, ligand_id: int, then) -> None:
        """Run `then()` once this ligand's poses on every column are known (one query, cached)."""
        if ligand_id in self._poses:
            then()
            return
        receptor_ids, protocol = [rid for rid, _ in self._columns], self._protocol

        def _fetch():
            grouped: dict[int, list[dict]] = {}
            for row in self.runtime.docking.offtarget_rows(
                    receptor_ids=receptor_ids, ligand_ids=[ligand_id], protocol_hash=protocol):
                grouped.setdefault(row["receptor_id"], []).append(row)
            return grouped

        def _apply(grouped):
            if protocol == self._protocol:
                self._poses[ligand_id] = grouped
                then()

        run_async(_fetch, _apply, lambda error: _log.warning("Could not read the poses: %s", error))

    def _select_pose(self, ligand_id: int, receptor_id: int, rank: int) -> None:
        if rank == self._ranks.get(receptor_id, 1):
            self._overrides.pop((ligand_id, receptor_id), None)
        else:
            self._overrides[(ligand_id, receptor_id)] = rank
        if self.table is not None:
            self.table.view.viewport().update()
        self._on_pose_changed(ligand_id, receptor_id)

    def _apply_pose(self) -> None:
        rank, column = self.pose_combo.currentData(), self.pose_column_combo.currentData()
        if rank is None or not self._columns:
            return
        for rid, _ in self._columns:
            if column is None or rid == column:
                self._ranks[rid] = int(rank)
        self._overrides = {key: r for key, r in self._overrides.items()
                           if column is not None and key[1] != column}
        self._rebuild(keep_scroll=True)
        if self._grid_ligand_id is not None:
            self._show_ligand_grid(self._grid_ligand_id)

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

    def _on_cell_clicked(self, index) -> None:
        # The ligand name shows every receptor + its selected pose in a PyMOL grid; a receptor
        # cell opens its poses.
        col_def = index.data(COLUMN_DEF_ROLE)
        row_data = index.model().get_row_data(index.row())
        if col_def is None or row_data is None or not self._columns:
            return
        ligand_id = int(row_data["__raw__"].ligand_molecule_id)
        self._names[ligand_id] = str(row_data.get("ligand_name") or ligand_id)
        if col_def.field == "ligand_name":
            self._show_ligand_grid(ligand_id)
            return
        rid = _receptor_of(col_def.field, self._columns[0][0])
        if rid is not None:
            view = self.table.view
            anchor = view.viewport().mapToGlobal(view.visualRect(index).bottomLeft())
            self._with_poses(ligand_id, lambda: self._pose_menu(ligand_id, rid, anchor))

    def _pose_menu(self, ligand_id: int, receptor_id: int, anchor) -> None:
        poses = self._poses.get(ligand_id, {}).get(receptor_id) or []
        if not poses or self.table is None:
            return
        current = self._rank(ligand_id, receptor_id)
        menu = QMenu(self.table)
        for pose in poses:
            score = _finite(pose["score"])
            action = QAction(f"P{pose['pose_rank']}   {'—' if score is None else f'{score:.2f}'} kcal/mol", menu)
            action.setCheckable(True)
            action.setChecked(pose["pose_rank"] == current)
            action.triggered.connect(
                lambda _c=False, rank=pose["pose_rank"]: self._select_pose(ligand_id, receptor_id, rank))
            menu.addAction(action)
        menu.popup(anchor)

    def _selected_pose(self, ligand_id: int, receptor_id: int) -> dict | None:
        rank = self._rank(ligand_id, receptor_id)
        return next((p for p in self._poses.get(ligand_id, {}).get(receptor_id, ()) if p["pose_rank"] == rank), None)

    def _on_pose_changed(self, ligand_id: int, receptor_id: int) -> None:
        pose = self._selected_pose(ligand_id, receptor_id)
        cmd, dock = self._pymol()
        if pose is None or cmd is None:
            return
        context = getattr(dock, "scene_context", None)
        objects = cmd.get_names("objects")
        if (context is not None and context.is_comparison and self._grid_ligand_id == ligand_id
                and f"rec_{receptor_id}" in objects and f"lig_{receptor_id}" in objects):
            self._replace_grid_pose(dock, ligand_id, receptor_id, pose)
        else:
            self._show_ligand_grid(ligand_id)

    def _pymol(self):
        dock = getattr(self.window(), "pymol_dock", None)
        return getattr(dock, "cmd", None) if dock is not None else None, dock

    def _show_ligand_grid(self, ligand_id: int) -> None:
        """Tile every receptor (with its selected pose) for this ligand in a PyMOL grid.
        With 'Align receptors' on, each receptor is superimposed onto the target and its
        pose is carried along by the same matrix, so binding modes are directly comparable."""
        cmd, dock = self._pymol()
        if cmd is None:
            return
        self._grid_request += 1
        request = self._grid_request
        self._with_poses(ligand_id, lambda: request == self._grid_request and self._fetch_grid(
            cmd, dock, ligand_id, request))

    def _fetch_grid(self, cmd, dock, ligand_id: int, request: int) -> None:
        ref_id = self._columns[0][0]
        wanted = {}
        complex_names = {}
        ligand_name = self._names.get(ligand_id, str(ligand_id))
        for rid, receptor_name in self._columns:
            pose = self._selected_pose(ligand_id, rid)
            if pose is not None and pose.get("receptor_payload_id") and pose.get("pose_payload_id"):
                wanted[f"rec_{rid}"] = int(pose["receptor_payload_id"])
                wanted[f"lig_{rid}"] = int(pose["pose_payload_id"])
                complex_names[rid] = f"{receptor_name}-{ligand_name}[P{pose.get('pose_rank') or 1}]"
        store = payload_store(self.runtime)

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

    def _replace_grid_pose(self, dock, ligand_id: int, receptor_id: int, pose: dict) -> None:
        payload_id = pose.get("pose_payload_id")
        if not payload_id:
            return
        receptor_name = dict(self._columns)[receptor_id]
        title = f"{receptor_name}-{self._names.get(ligand_id, ligand_id)}[P{pose.get('pose_rank') or 1}]"
        rec_obj, lig_obj = f"rec_{receptor_id}", f"lig_{receptor_id}"
        store = payload_store(self.runtime)

        def draw(found: dict) -> None:
            selected = self._selected_pose(ligand_id, receptor_id)
            if (self._grid_ligand_id != ligand_id or selected is None
                    or selected.get("pose_payload_id") != payload_id
                    or selected.get("pose_rank") != pose.get("pose_rank")
                    or not dock.scene_context.is_comparison):
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
