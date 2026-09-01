from __future__ import annotations

import json
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.docking.result_tables import (
    fmt_float,
    fmt_sci,
    results_ligand_config,
    results_pose_config,
    results_receptor_config,
)
from amdockvs.models import DockingResultRecord, MoleculeRecord
from amdockvs.summaries import DockingHitSummary
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.async_query import run_async
from amdockvs.ui.catalog.common import BoundTableWidget as ProjectBoundTableWidget, project_table
from ms_components.ms_dockwidget.widget import DockManager, MSDockWidget
from amdockvs.vocab import FileFormat, MoleculeType, MoleculeUsageClass
from ms_components.ms_table import AlignHint, ColumnDef, ColumnKind, FilterOperator, FilterSpec, MetricFilterBar, SmartTableView, SortSpec, TableConfig, TableLoadMode, choices_from_class

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - optional UI dependency
    pg = None



def _receptor_table_config() -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("name", label="Name", width=220, sortable=True, filterable=True),
            ColumnDef("molecule_type", label="Type", width=140, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeType)),
            ColumnDef("usage_class", label="Usage", width=100, sortable=True, filterable=True, visible=False,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeUsageClass)),
            ColumnDef("source_index", label="Source Idx", width=100, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("n_atoms", label="Atoms", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("input_format", label="Format", width=90, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(FileFormat)),
            ColumnDef("source", label="Source File", width=300, sortable=True),
            ColumnDef("stored_path", label="Stored Path", width=300, sortable=True, visible=False),
        ],
        default_filters=[
            FilterSpec("is_receptor", FilterOperator.EQ, True, label="role_receptor"),
            FilterSpec("usage_class", FilterOperator.EQ, "general", label="general_only"),
            FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only"),
        ],
        # default_sort=[SortSpec("id", descending=True)],
        page_size=20,
        page_size_options=[10, 20, 50, 100],
        show_row_numbers=True,
        multi_select=True,
        empty_message="No receptors loaded in the active project",
    )


#: Fields the results view can filter on, as (key, label). The key is what SQL understands:
#: "score" is the column, the rest are JSON metrics of the same row.
_RESULT_FILTER_FIELDS = (
    ("score", "Score"),
    ("ligand_efficiency", "LE"),
    ("predicted_pki", "pKi"),
    ("lipophilic_efficiency", "LLE"),
    ("fit_quality", "Fit quality"),
    ("bei", "BEI"),
    ("sei", "SEI"),
)



def _fmt_path(value) -> str:
    return "" if value is None else str(value)


class _MetricColumns:
    """Columns of a result table, with a right-click chooser on the header.

    The metrics already travel in every hit; what was missing was somewhere to put them without
    a wall of columns, so each one is a spec `(label, getter, visible_by_default)` and the header
    menu turns it on. Column 0 is the identity of the row and is never hidden.
    """

    def __init__(self, table, specs):
        self.table = table
        self.specs = tuple(specs)
        table.setColumnCount(len(self.specs))
        table.setHorizontalHeaderLabels([label for label, _getter, _shown in self.specs])
        for index, (_label, _getter, shown) in enumerate(self.specs):
            table.setColumnHidden(index, not shown)
        header = table.horizontalHeader()
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_menu)
        header.setToolTip("Right-click: choose columns")

    def fill(self, row_index: int, context) -> None:
        for index, (_label, getter, _shown) in enumerate(self.specs):
            self.table.setItem(row_index, index, QTableWidgetItem(getter(context)))

    def _show_menu(self, position) -> None:
        menu = QMenu(self.table)
        for index, (label, _getter, _shown) in enumerate(self.specs):
            if index == 0:
                continue  # the row's identity: hiding it would leave rows nobody can read
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(index))
            action.toggled.connect(lambda shown, col=index: self._set_visible(col, shown))
        menu.exec(self.table.horizontalHeader().mapToGlobal(position))

    def _set_visible(self, column: int, shown: bool) -> None:
        self.table.setColumnHidden(column, not shown)
        self.table.resizeColumnsToContents()


class StatCard(QGroupBox):
    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        # layout.setContentsMargins(10, 10, 10, 10)
        self.value_label = QLabel("-", self)
        self.value_label.setObjectName("statValue")
        self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class DockingResultsWidget(QWidget):
    data_refreshed = Signal(bool)
    filters_changed = Signal()

    def __init__(self, *, runtime, load_hit_in_pymol: Callable[[DockingHitSummary, int], None] | None = None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._load_hit_in_pymol = load_hit_in_pymol
        self._selected_receptor_id: int | None = None
        self._selected_ligand_id: int | None = None
        self._selected_hit: DockingHitSummary | None = None
        self._hit_token = 0
        self._protocol_token = 0
        self._receptor_refreshing = False
        self._ligand_refreshing = False
        self._auto_select_ligand = False
        self._auto_select_pose = False
        self.filters_changed.connect(lambda: self._refresh_ligands(auto_select=True))

        outer = QVBoxLayout(self)
        # outer.setContentsMargins(0, 0, 0, 0)
        outer.setContentsMargins(0, 5, 0, 5)
        # outer.setSpacing(8)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to inspect docking results.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        outer.addWidget(self._build_filter_panel(self))

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        # Navigation is always scoped to one receptor. An all-receptor ligand aggregation grows
        # without bound and, more importantly, hides newly completed receptor-ligand pairs behind
        # the same ligand row.
        receptor_box = QWidget(self)
        receptor_layout = QVBoxLayout(receptor_box)
        receptor_layout.setContentsMargins(0, 0, 0, 0)
        receptor_layout.addWidget(QLabel("Receptor", self))
        self.receptor_table = project_table(runtime, results_receptor_config(), receptor_box)
        self.receptor_table.setMinimumHeight(150)
        self.receptor_table.selection_changed.connect(self._on_receptor_selection_changed)
        self.receptor_table.data_refreshed.connect(self._on_receptors_loaded)
        receptor_layout.addWidget(self.receptor_table)
        splitter.addWidget(receptor_box)

        # Ligands docked against the selected receptor (one row per ligand = its best pose).
        ligand_box = QWidget(self)
        ligand_layout = QVBoxLayout(ligand_box)
        ligand_layout.setContentsMargins(0, 0, 0, 0)
        self.ligand_label = QLabel("Ligand", self)
        ligand_layout.addWidget(self.ligand_label)
        self.ligand_table = project_table(runtime, results_ligand_config(), ligand_box)
        self.ligand_table.setMinimumHeight(150)
        self.ligand_table.selection_changed.connect(self._on_ligand_selection_changed)
        self.ligand_table.data_refreshed.connect(self._on_ligands_loaded)
        ligand_layout.addWidget(self.ligand_table)
        splitter.addWidget(ligand_box)

        # Poses of the selected ligand (the N modes inside its docking output).
        pose_box = QWidget(self)
        pose_layout = QVBoxLayout(pose_box)
        pose_layout.setContentsMargins(0, 0, 0, 0)
        pose_layout.addWidget(QLabel("Pose", self))
        self.pose_table = project_table(runtime, results_pose_config(), pose_box)
        self.pose_table.setMinimumHeight(150)
        self.pose_table.selection_changed.connect(self._on_pose_selection_changed)
        self.pose_table.data_refreshed.connect(self._on_poses_loaded)
        pose_layout.addWidget(self.pose_table)
        splitter.addWidget(pose_box)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)

        # "Selected Result" lives in the window's auxiliary zone, not under the tables: it is
        # detail about the row you clicked, which is exactly what that zone is for. Built here
        # (it is wired to this widget's tables) and handed over by `aux_panel`, parentless until
        # the window mounts it.
        detail_scroll = QScrollArea(None)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.NoFrame)
        detail_scroll.setWidget(self._build_detail_panel(detail_scroll))
        self._aux_panel = detail_scroll
        self._on_receptors_loaded()

    def aux_panel(self) -> QWidget | None:
        """The window's auxiliary zone asks every tab for this; None = it has nothing to show."""
        return getattr(self, "_aux_panel", None)

    @staticmethod
    def _init_table(table: QTableWidget) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)

    def _build_detail_panel(self, parent: QWidget) -> QWidget:
        box = QGroupBox("Selected Result", parent)
        layout = QVBoxLayout(box)

        self.detail_placeholder = QLabel("Select a docking result to inspect it.", box)
        self.detail_placeholder.setWordWrap(True)
        layout.addWidget(self.detail_placeholder)

        # Structured key fields — the important stuff, not a JSON dump.
        self.detail_fields = QWidget(box)
        form = QFormLayout(self.detail_fields)
        form.setContentsMargins(0, 0, 0, 0)
        self.d_score = QLabel("-", self.detail_fields)
        self.d_score.setObjectName("statValue")
        self.d_status = QLabel("-", self.detail_fields)
        self.d_ligand = QLabel("-", self.detail_fields)
        self.d_ligand.setWordWrap(True)
        self.d_receptor = QLabel("-", self.detail_fields)
        self.d_receptor.setWordWrap(True)
        self.d_error = QLabel("-", self.detail_fields)
        self.d_error.setWordWrap(True)
        self._d_error_caption = QLabel("Error", self.detail_fields)
        form.addRow("Score", self.d_score)
        form.addRow("Status", self.d_status)
        form.addRow("Ligand", self.d_ligand)
        form.addRow("Receptor", self.d_receptor)
        form.addRow(self._d_error_caption, self.d_error)
        self.detail_fields.setVisible(False)
        layout.addWidget(self.detail_fields)

        # "See more" — the low-relevance data (ids, paths, metadata), collapsed by default.
        self.detail_more_btn = QToolButton(box)
        self.detail_more_btn.setText("Show details")
        self.detail_more_btn.setCheckable(True)
        self.detail_more_btn.setArrowType(Qt.RightArrow)
        self.detail_more_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.detail_more_btn.toggled.connect(self._on_toggle_detail_more)
        self.detail_more_btn.setVisible(False)
        layout.addWidget(self.detail_more_btn)

        self.detail_more = QTextEdit(box)
        self.detail_more.setReadOnly(True)
        self.detail_more.setVisible(False)
        self.detail_more.setMaximumHeight(160)
        layout.addWidget(self.detail_more)

        # Interactions and the 2D diagram are one pass over the same pose, and the pose is what
        # this panel owns (C6) -- so the button is here, not in the diagram dock, which stays a
        # pure renderer of the saved JSON.
        self.interactions_box = QGroupBox("Interactions", box)
        interactions_layout = QVBoxLayout(self.interactions_box)
        header = QHBoxLayout()
        self.interactions_status = QLabel("Select a pose to load interactions.", self.interactions_box)
        self.interactions_status.setWordWrap(True)
        header.addWidget(self.interactions_status, 1)
        self.build_diagram_btn = QPushButton("Build diagram", self.interactions_box)
        self.build_diagram_btn.setToolTip(
            "Detect the interactions of this pose and solve its 2D layout (a few seconds), "
            "caching both next to the pose. The diagram opens in the 2D Interactions dock."
        )
        self.build_diagram_btn.setEnabled(False)
        self.build_diagram_btn.clicked.connect(self._build_diagram)
        header.addWidget(self.build_diagram_btn)
        interactions_layout.addLayout(header)
        self.interactions_text = QTextEdit(self.interactions_box)
        self.interactions_text.setReadOnly(True)
        self.interactions_text.setMinimumHeight(90)
        self.interactions_text.setMaximumHeight(180)
        interactions_layout.addWidget(self.interactions_text)
        layout.addWidget(self.interactions_box)
        layout.addStretch(1)
        return box

    def _build_analytics_panel(self, parent: QWidget) -> QWidget:
        box = QGroupBox("Analytics", parent)
        layout = QVBoxLayout(box)
        if pg is None:
            label = QLabel("pyqtgraph is not available; analytics plots are disabled.", box)
            label.setWordWrap(True)
            layout.addWidget(label)
            return box

        controls = QHBoxLayout()
        self.metric_plot_combo = QComboBox(box)
        self.metric_plot_combo.addItem("Score vs pKi", "predicted_pki")
        self.metric_plot_combo.addItem("Score vs LE", "ligand_efficiency")
        self.metric_plot_combo.addItem("Score vs LLE/LiPE", "lipophilic_efficiency")
        self.metric_plot_combo.addItem("Score vs Fit Quality", "fit_quality")
        self.metric_plot_combo.currentIndexChanged.connect(self._update_metric_plot)
        refresh_interactions = QPushButton("Refresh interaction charts", box)
        refresh_interactions.clicked.connect(self._refresh_interaction_stats)
        controls.addWidget(self.metric_plot_combo)
        controls.addWidget(refresh_interactions)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.metric_plot = pg.PlotWidget(background=None)
        self.metric_plot.setMinimumHeight(160)
        self.metric_plot.showGrid(x=True, y=True, alpha=0.25)
        layout.addWidget(self.metric_plot)

        charts = QHBoxLayout()
        self.interaction_type_plot = pg.PlotWidget(background=None)
        self.interaction_residue_plot = pg.PlotWidget(background=None)
        for plot in (self.interaction_type_plot, self.interaction_residue_plot):
            plot.setMinimumHeight(160)
            plot.showGrid(x=False, y=True, alpha=0.25)
            charts.addWidget(plot)
        layout.addLayout(charts)
        return box

    def _build_filter_panel(self, parent: QWidget) -> QWidget:
        box = QGroupBox("Filters", parent)
        layout = QVBoxLayout(box)
        top = QHBoxLayout()
        # Program/protocol isolation: different programs report different pose counts and
        # scoring units, so mixing them in one receptor>ligand>pose tree is apples-to-oranges.
        # Default lands on the first concrete protocol; "All (mixed)" is an explicit opt-in.
        self.protocol_combo = QComboBox(box)
        self.protocol_combo.setToolTip("Restrict results to one docking program/protocol (scores aren't comparable across programs).")
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        top.addWidget(QLabel("Protocol", box))
        top.addWidget(self.protocol_combo)
        # A failed pair still writes a row (score NULL) so the failure is visible; this hides
        # those rows. It is a state filter, not a metric, hence a checkbox and not a chip.
        self.hide_failed_check = QCheckBox("Hide failed poses", box)
        self.hide_failed_check.setToolTip("Hide result rows with no score (the docking failed for that pair).")
        self.hide_failed_check.toggled.connect(lambda _checked: self.filters_changed.emit())
        top.addWidget(self.hide_failed_check)
        top.addStretch(1)
        clear_btn = QPushButton("Clear", box)
        clear_btn.setToolTip("Remove status and metric filters; keep the selected protocol.")
        clear_btn.clicked.connect(self._clear_filters)
        top.addWidget(clear_btn)
        layout.addLayout(top)
        # Metrics filter as chips: one click adds the condition and the tables reload, two chips
        # on the same field are a range, and what is applied is what you can read on screen.
        self.metric_filters = MetricFilterBar(
            _RESULT_FILTER_FIELDS,
            box,
            default_value=-8.5,  # a plausible vina score: the common first filter needs no typing
            empty_text="No filters - showing every result.",
        )
        self.metric_filters.changed.connect(self.filters_changed.emit)
        layout.addWidget(self.metric_filters)
        return box

    def _clear_filters(self) -> None:
        # One state transition, one query. The protocol is the scientific scope and is kept;
        # Clear only removes state/metric filters.
        self.hide_failed_check.blockSignals(True)
        self.metric_filters.blockSignals(True)
        self.hide_failed_check.setChecked(False)
        self.metric_filters.clear()
        self.metric_filters.blockSignals(False)
        self.hide_failed_check.blockSignals(False)
        self.filters_changed.emit()

    def _on_toggle_detail_more(self, checked: bool) -> None:
        self.detail_more.setVisible(checked)
        self.detail_more_btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.detail_more_btn.setText("Hide details" if checked else "Show details")

    def _loaded_objects(self, table: SmartTableView) -> list:
        return [
            obj for row in table._model.loaded_rows
            if (obj := table._model.get_raw_object(row)) is not None
        ]

    @staticmethod
    def _select_id(table: SmartTableView, object_id: int | None, *, notify: bool) -> bool:
        if object_id is None:
            return False
        target = next(
            (
                row for row in table._model.loaded_rows
                if int(getattr(table._model.get_raw_object(row), "id", 0) or 0) == int(object_id)
            ),
            -1,
        )
        if target < 0:
            return False
        selection = table._table.selectionModel()
        selection.blockSignals(not notify)
        try:
            table._table.selectRow(target)
        finally:
            selection.blockSignals(False)
        return True

    def _on_receptors_loaded(self, *_args) -> None:
        rows = [
            self.receptor_table._model.get_row_data(row)
            for row in self.receptor_table._model.loaded_rows
        ]
        signature = tuple(
            (
                int(getattr(row.get("__raw__"), "id", 0) or 0),
                row.get("docked"), row.get("done"), row.get("missing"),
            )
            for row in rows if row is not None
        )
        old_signature = getattr(self, "_receptor_signature", None)
        self._receptor_signature = signature
        if self._receptor_refreshing:
            self.data_refreshed.emit(old_signature is not None and signature != old_signature)
        if self._select_id(self.receptor_table, self._selected_receptor_id, notify=False):
            return
        objects = self._loaded_objects(self.receptor_table)
        if objects:
            self._select_id(self.receptor_table, int(objects[0].id or 0), notify=False)
            self._on_receptor_selection_changed([objects[0]])
            return
        self._selected_receptor_id = None
        self._selected_ligand_id = None
        self._show_detail(None)

    @staticmethod
    def _pose_rank(hit: DockingHitSummary) -> int:
        # The pose's state index in the output SDF; metadata.pose_index is 0-based.
        try:
            return int(hit.metadata.get("pose_index", 0)) + 1
        except (TypeError, ValueError):
            return 1

    def _reload_protocols(self, *, auto_select: bool, preserve_view: bool = False) -> None:
        # Cheap DISTINCT query drives the combo; then the scoped ligand fetch runs. Kept off the
        # GUI thread so a project with many results never blocks on it.
        receptor_id = self._selected_receptor_id
        self._protocol_token += 1
        token = self._protocol_token
        run_async(
            lambda: self.runtime.docking.result_protocols(receptor_id=receptor_id),
            lambda protocols: self._apply_protocols(protocols, auto_select, preserve_view, token),
            on_error=lambda _exc: self._apply_protocols([], auto_select, preserve_view, token),
        )

    def _apply_protocols(
        self,
        protocols,
        auto_select: bool,
        preserve_view: bool = False,
        token: int | None = None,
    ) -> None:
        if token is not None and token != self._protocol_token:
            return
        old_protocol = str(self.protocol_combo.currentData() or "")
        self._populate_protocol_combo(list(protocols or []))
        if preserve_view and str(self.protocol_combo.currentData() or "") == old_protocol:
            self._ligand_refreshing = True
            try:
                self.ligand_table.refresh_preserving_view()
            finally:
                self._ligand_refreshing = False
            return
        self._refresh_ligands(auto_select=auto_select)

    def _populate_protocol_combo(self, protocols: list[tuple[str, str]]) -> None:
        combo = self.protocol_combo
        current = str(combo.currentData() or "") if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("All protocols (mixed)", "")
        for phash, label in protocols:
            combo.addItem(label, phash)
        idx = combo.findData(current) if current else -1
        if idx < 0:
            idx = 1 if combo.count() > 1 else 0  # default to first concrete protocol, not mixed
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _ligand_filters(self) -> list[FilterSpec]:
        filters = [
            FilterSpec("receptor_molecule_id", FilterOperator.EQ, self._selected_receptor_id or -1),
            FilterSpec("pose_rank", FilterOperator.EQ, 1),
            FilterSpec("run_kind", FilterOperator.NEQ, "redocking"),
        ]
        protocol_hash = str(self.protocol_combo.currentData() or "")
        if protocol_hash:
            filters.append(FilterSpec("protocol_hash", FilterOperator.EQ, protocol_hash))
        if self.hide_failed_check.isChecked():
            filters.append(FilterSpec("score", FilterOperator.NOT_NULL, None))
        operators = {"gte": FilterOperator.GTE, "lte": FilterOperator.LTE}
        filters.extend(
            FilterSpec(field, operators[op], value)
            for field, op, value in self.metric_filters.conditions()
            if op in operators
        )
        return filters

    def _refresh_ligands(self, *, auto_select: bool = False, preserve_context: bool = False) -> None:
        if not preserve_context:
            self._selected_ligand_id = None
            self._show_detail(None)
        self._auto_select_ligand = auto_select
        self._ligand_refreshing = True
        try:
            self.ligand_table.set_filters(self._ligand_filters())
        finally:
            self._ligand_refreshing = False

    def _on_ligands_loaded(self, *_args) -> None:
        rows = self._loaded_objects(self.ligand_table)
        signature = tuple((int(row.id or 0), row.score) for row in rows)
        old_signature = getattr(self, "_ligand_signature", None)
        self._ligand_signature = signature
        if self._ligand_refreshing:
            self.data_refreshed.emit(old_signature is not None and signature != old_signature)
        if self._select_ligand_id(self._selected_ligand_id, notify=False):
            return
        if self._auto_select_ligand and rows:
            self._select_ligand_id(int(rows[0].ligand_molecule_id or 0), notify=False)
            self._on_ligand_selection_changed([rows[0]])
            return
        if not rows:
            self._selected_ligand_id = None
            self._show_detail(None)

    def _select_ligand_id(self, ligand_id: int | None, *, notify: bool) -> bool:
        target = next(
            (
                row for row in self.ligand_table._model.loaded_rows
                if int(getattr(self.ligand_table._model.get_raw_object(row), "ligand_molecule_id", 0) or 0)
                == int(ligand_id or 0)
            ),
            -1,
        )
        if target < 0:
            return False
        selection = self.ligand_table._table.selectionModel()
        selection.blockSignals(not notify)
        try:
            self.ligand_table._table.selectRow(target)
        finally:
            selection.blockSignals(False)
        return True

    def _on_protocol_changed(self, _index: int) -> None:
        self._refresh_ligands(auto_select=True)

    def _on_receptor_selection_changed(self, objects: list) -> None:
        if not objects:
            if not self._receptor_refreshing:
                self._selected_receptor_id = None
            return
        receptor_id = int(getattr(objects[0], "id", 0) or 0)
        if receptor_id == self._selected_receptor_id:
            return
        self._selected_receptor_id = receptor_id or None
        self._selected_ligand_id = None
        self._show_detail(None)
        self._reload_protocols(auto_select=True)

    @staticmethod
    def _raw_protocol_hash(result) -> str:
        return str(((getattr(result, "metrics", {}) or {}).get("protocol") or {}).get("hash") or "")

    @staticmethod
    def _raw_run_id(result) -> str:
        return str((getattr(result, "metrics", {}) or {}).get("run_id") or "")

    def _on_ligand_selection_changed(self, objects: list) -> None:
        if not objects:
            if not self._ligand_refreshing:
                self._selected_ligand_id = None
            return
        result = objects[0]
        ligand_id = int(getattr(result, "ligand_molecule_id", 0) or 0)
        self._selected_ligand_id = ligand_id or None
        filters = [
            FilterSpec("receptor_molecule_id", FilterOperator.EQ, self._selected_receptor_id or -1),
            FilterSpec("ligand_molecule_id", FilterOperator.EQ, ligand_id or -1),
            FilterSpec("run_kind", FilterOperator.NEQ, "redocking"),
        ]
        protocol_hash = self._raw_protocol_hash(result)
        if protocol_hash:
            filters.append(FilterSpec("protocol_hash", FilterOperator.EQ, protocol_hash))
        run_id = self._raw_run_id(result)
        if run_id:
            filters.append(FilterSpec("run_id", FilterOperator.EQ, run_id))
        self._auto_select_pose = True
        self.pose_table.set_filters(filters)

    def _on_poses_loaded(self, *_args) -> None:
        if self._auto_select_pose:
            self._auto_select_pose = False
            rows = self._loaded_objects(self.pose_table)
            if rows:
                self._select_id(self.pose_table, int(rows[0].id or 0), notify=False)
                self._on_pose_selection_changed([rows[0]])

    def _on_pose_selection_changed(self, objects: list) -> None:
        if not objects:
            return
        result_id = int(getattr(objects[0], "id", 0) or 0)
        if result_id <= 0:
            return
        self._hit_token += 1
        token = self._hit_token
        run_async(
            lambda: self.runtime.docking.hit(result_id=result_id),
            lambda hit: self._apply_selected_hit(hit, token),
            on_error=lambda _exc: self._apply_selected_hit(None, token),
        )

    def _apply_selected_hit(self, hit: DockingHitSummary | None, token: int) -> None:
        if token != self._hit_token:
            return
        self._show_detail(hit)
        self._load_selected_hit_in_pymol()

    def ensure_viewport_filled(self, force: bool = False) -> bool:
        """During a job it only refreshes the first window until Ligands is filled."""
        now = time.monotonic()
        if now - getattr(self, "_last_fill_refresh", 0.0) < 0.75:
            return False
        self._last_fill_refresh = now
        if self.receptor_table._model.loaded_count == 0:
            self._receptor_refreshing = True
            try:
                self.receptor_table.refresh()
            finally:
                self._receptor_refreshing = False
            return bool(force and self.receptor_table._model.loaded_count > 0)
        if self._selected_receptor_id is None:
            self.receptor_table.select_first_row()
            return False
        capacity = self.ligand_table._viewport_row_capacity()
        if self.ligand_table._model.loaded_count >= capacity:
            return True
        self._refresh_ligands(auto_select=self._selected_ligand_id is None, preserve_context=True)
        return bool(force or self.ligand_table._model.loaded_count >= capacity)

    def refresh_counts(self) -> bool:
        # Receptors contain live aggregate columns (Docked/Done/Missing), so refresh that
        # small window while preserving selection.  Ligands only needs its COUNT refreshed:
        # rebuilding its score-sorted rows would move the user's selection and reload PyMOL.
        self._receptor_refreshing = True
        try:
            self.receptor_table.refresh_preserving_view()
        finally:
            self._receptor_refreshing = False
        return bool(self.ligand_table.refresh_counts())

    def refresh_view(self) -> None:
        self._receptor_refreshing = True
        try:
            self.receptor_table.refresh_preserving_view()
        finally:
            self._receptor_refreshing = False
        if self._selected_receptor_id is not None:
            # Protocols are the scientific scope. Re-query them once on a manual/final refresh,
            # not on every monitor tick, then preserve the table window when the scope is equal.
            self._reload_protocols(auto_select=False, preserve_view=True)

    refresh_results_view = refresh_view

    def _show_detail(self, hit: DockingHitSummary | None) -> None:
        self._selected_hit = hit
        if hit is None:
            self.detail_placeholder.setVisible(True)
            self.detail_fields.setVisible(False)
            self.detail_more_btn.setVisible(False)
            self.detail_more.setVisible(False)
            self.interactions_status.setText("Select a pose to load interactions.")
            self.interactions_text.clear()
            self.build_diagram_btn.setEnabled(False)
            return
        self.detail_placeholder.setVisible(False)
        self.detail_fields.setVisible(True)
        self.detail_more_btn.setVisible(True)
        self.d_score.setText(fmt_float(hit.score))
        self.d_status.setText(hit.status or "-")
        self.d_ligand.setText(hit.ligand_name or f"Ligand {hit.ligand_id}")
        self.d_receptor.setText(hit.receptor_name or f"Receptor {hit.receptor_id}")
        has_error = bool(hit.error)
        self._d_error_caption.setVisible(has_error)
        self.d_error.setVisible(has_error)
        self.d_error.setText(hit.error or "")
        more = {
            "result_id": hit.result_id,
            "complex_id": hit.complex_id,
            "run_kind": hit.run_kind,
            "ligand_id": hit.ligand_id,
            "receptor_id": hit.receptor_id,
            "ligand_path": _fmt_path(hit.ligand_path),
            "receptor_path": _fmt_path(hit.receptor_path),
            "output_path": _fmt_path(hit.output_path),
            "updated_at": None if hit.updated_at is None else str(hit.updated_at),
            "metadata": hit.metadata,
            "metrics": {
                "LE": hit.ligand_efficiency,
                "predicted Ki (M)": hit.predicted_ki_m,
                "predicted pKi": hit.predicted_pki,
                "LLE/LiPE": hit.lipophilic_efficiency,
                "FQ": hit.fit_quality,
                "BEI": hit.bei,
                "SEI": hit.sei,
            },
        }
        self.detail_more.setPlainText(json.dumps(more, indent=2, default=str))
        self._load_interactions(hit)

    def _load_selected_hit_in_pymol(self) -> None:
        if self._selected_hit is None or self._load_hit_in_pymol is None:
            return
        self._load_hit_in_pymol(self._selected_hit, self._pose_rank(self._selected_hit))

    def _loaded_result_ids(self) -> list[int]:
        # The best pose of every ligand on screen, plus the poses of the selected one. Interactions
        # and diagrams for a whole screening are a job with its own SQL filters, not a click here.
        ids = {int(hit.result_id) for hit in self._all_loaded_hits() if int(hit.result_id or 0) > 0}
        return sorted(ids)

    @staticmethod
    def _metric_value(hit: DockingHitSummary, key: str) -> float | None:
        value = getattr(hit, key, None)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _all_loaded_hits(self) -> list[DockingHitSummary]:
        # Dynamic tables retain only their visible window, not an in-memory result set.
        return [self._selected_hit] if self._selected_hit is not None else []

    def _update_metric_plot(self) -> None:
        if pg is None or not hasattr(self, "metric_plot"):
            return
        metric_key = str(self.metric_plot_combo.currentData() or "predicted_pki")
        points = [
            (float(hit.score), float(metric))
            for hit in self._all_loaded_hits()
            if hit.score is not None
            for metric in [self._metric_value(hit, metric_key)]
            if metric is not None
        ]
        self.metric_plot.clear()
        self.metric_plot.setLabel("bottom", "Score (kcal/mol)")
        self.metric_plot.setLabel("left", self.metric_plot_combo.currentText().replace("Score vs ", ""))
        if not points:
            self.metric_plot.setTitle("No metric data for current filters")
            return
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        self.metric_plot.setTitle(f"{len(points)} point(s) — loaded ligands, best pose")
        self.metric_plot.plot(
            x_values,
            y_values,
            pen=None,
            symbol="o",
            symbolSize=7,
            symbolBrush=(44, 128, 184, 180),
        )

    def _refresh_interaction_stats(self) -> None:
        if pg is None:
            return
        result_ids = self._loaded_result_ids()
        if not result_ids:
            self._show_interaction_stats({"by_type": [], "by_residue": []})
            return
        run_async(
            lambda: self.runtime.docking.interaction_stats(result_ids=result_ids),
            self._show_interaction_stats,
            on_error=lambda _exc: self._show_interaction_stats({"by_type": [], "by_residue": []}),
        )

    def _plot_bar_rows(self, plot, rows: list[dict], *, title: str) -> None:
        if pg is None:
            return
        plot.clear()
        plot.setTitle(title)
        plot.setLabel("left", "Hits")
        plot.setLabel("bottom", "")
        if not rows:
            return
        trimmed = list(rows[:12])
        x_values = list(range(len(trimmed)))
        heights = [int(row.get("hit_count") or 0) for row in trimmed]
        bar = pg.BarGraphItem(x=x_values, height=heights, width=0.65, brush=(90, 139, 84, 180))
        plot.addItem(bar)
        axis = plot.getAxis("bottom")
        axis.setTicks([[(index, str(row.get("label") or "-")) for index, row in enumerate(trimmed)]])
        plot.setXRange(-0.75, max(0.75, len(trimmed) - 0.25), padding=0)

    def _show_interaction_stats(self, stats) -> None:
        if pg is None or not hasattr(self, "interaction_type_plot"):
            return
        stats = dict(stats or {})
        self._plot_bar_rows(
            self.interaction_type_plot,
            list(stats.get("by_type") or []),
            title="Hits vs interaction type",
        )
        self._plot_bar_rows(
            self.interaction_residue_plot,
            list(stats.get("by_residue") or []),
            title="Hits vs residue",
        )

    def _load_interactions(self, hit: DockingHitSummary) -> None:
        # Our own detection writes them next to the pose, so listing them is a plain JSON read
        # (a few kB, no ms_contactmap import): no DB round-trip and no worker to freeze the panel.
        from amdockvs.docking.diagram import pose_interactions

        ready = bool(hit.output_path and hit.receptor_path)
        self.build_diagram_btn.setEnabled(ready)
        rows = pose_interactions(str(hit.output_path), self._pose_rank(hit)) if hit.output_path else None
        self._show_interactions(rows)

    def _show_interactions(self, rows: list[dict] | None) -> None:
        self.interactions_text.clear()
        if rows is None:
            self.interactions_status.setText("No diagram for this pose yet - press Build diagram.")
            return
        if not rows:
            self.interactions_status.setText("No interactions detected for this pose.")
            return
        counts: dict[str, int] = {}
        lines: list[str] = []
        for row in rows:
            kind = str(row.get("type") or "interaction")
            counts[kind] = counts.get(kind, 0) + 1
            residue = str(row.get("residue") or "-")
            lines.append(f"{kind:14s} {residue:12s} {fmt_float(row.get('distance_angstrom'))} A")
        summary = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        self.interactions_status.setText(f"{len(rows)} interaction(s): {summary}")
        self.interactions_text.setPlainText("\n".join(lines[:200]))

    def _build_diagram(self) -> None:
        hit = self._selected_hit
        if hit is None or not hit.output_path or not hit.receptor_path:
            return
        # Widget-free captures: detection + layout solve run off the GUI thread. The solve is
        # seconds of numpy, so it rides along in the worker instead of blocking the window.
        pose_path, receptor_path = str(hit.output_path), str(hit.receptor_path)
        rank, result_id = self._pose_rank(hit), int(hit.result_id or 0)
        label = f"{hit.ligand_name} · pose {rank}"
        self.build_diagram_btn.setEnabled(False)
        self.interactions_status.setText("Building diagram...")

        def work():
            from amdockvs.docking.diagram import build_pose_diagram, save_pose_diagram

            diagram = build_pose_diagram(
                pose_path=pose_path, receptor_path=receptor_path, pose_rank=rank, name=label
            )
            if diagram is None:
                return None
            from ms_contactmap import solve_layout

            save_pose_diagram(pose_path, rank, diagram, solve_layout(diagram))
            return True

        run_async(
            work,
            lambda built: self._on_diagram_built(result_id, "" if built else "No interactions found."),
            on_error=lambda exc: self._on_diagram_built(result_id, str(exc)),
        )

    def _on_diagram_built(self, result_id: int, error: str) -> None:
        self.build_diagram_btn.setEnabled(True)
        hit = self._selected_hit
        if hit is None or int(hit.result_id or 0) != result_id:
            return  # the user moved on; whatever is selected now already shows its own state
        if error:
            self.interactions_status.setText(error)
            return
        self._load_interactions(hit)
        dock = getattr(self.window(), "diagram_dock", None)
        if dock is not None:
            dock.reload()


class LigandActivityWidget(QWidget):
    """Activity editor: pick ligands, type/normalize values, or bulk-load from CSV. Edits write
    straight through runtime.qsar.set_activity; the same table renders CSV-loaded values."""

    def __init__(self, *, runtime, open_results_view: Callable[[], None] | None = None,
                 show_histogram: Callable[[str, tuple], None] | None = None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._open_results_view = open_results_view
        self._show_histogram = show_histogram
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to edit ligand activities.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        from amdockvs.ui.tools.qsar.activities import TRANSFORM_CHOICES, UNIT_CHOICES

        self.endpoint_combo = QComboBox(self)
        self.endpoint_combo.setEditable(True)
        self.endpoint_combo.setMinimumWidth(140)
        self.endpoint_combo.currentTextChanged.connect(lambda _t: self.refresh_view())
        self.unit_combo = QComboBox(self)
        self.unit_combo.setEditable(True)
        self.unit_combo.addItems(UNIT_CHOICES)
        self.transform_combo = QComboBox(self)
        self.transform_combo.addItems(TRANSFORM_CHOICES)
        self.model_combo = QComboBox(self)
        self.model_combo.setMinimumWidth(160)
        self.model_combo.setToolTip("Show which train/test subset each ligand fell into for this model.")
        self.model_combo.currentIndexChanged.connect(lambda _i: self.refresh_view())
        add_btn = QPushButton("Add ligands…", self)
        add_btn.clicked.connect(self._add_ligands)
        csv_btn = QPushButton("Load from CSV…", self)
        csv_btn.clicked.connect(self._load_csv)
        matrix_btn = QPushButton("Load matrix…", self)
        matrix_btn.setToolTip("Load many endpoints at once from a wide CSV (e.g. Tox21's 12 assays). "
                              "Import the ligands first, then match by name/structure.")
        matrix_btn.clicked.connect(self._load_matrix)
        norm_btn = QPushButton("Normalize to pIC50", self)
        norm_btn.setToolTip("Re-load the current endpoint applying the selected unit + transform.")
        norm_btn.clicked.connect(self._normalize)
        del_btn = QPushButton("Delete selected", self)
        del_btn.clicked.connect(self._delete_selected)
        # Two rows so the toolbar's minimum width is ~half — otherwise a single long row forces a
        # large minimum width on this widget and locks every dock to its right (PyMOL, the chart)
        # at that width. Row 1: action buttons (right). Row 2: endpoint controls (left).
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        for w in (add_btn, csv_btn, matrix_btn, norm_btn, del_btn):
            buttons_row.addWidget(w)
        outer.addLayout(buttons_row)

        controls_row = QHBoxLayout()
        for w in (QLabel("Endpoint:"), self.endpoint_combo, QLabel("Unit:"), self.unit_combo,
                  QLabel("Transform:"), self.transform_combo, QLabel("Subset for model:"), self.model_combo):
            controls_row.addWidget(w)
        controls_row.addStretch(1)
        outer.addLayout(controls_row)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Ligand id", "Name", "Value", "Unit", "Subset"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self.table, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.refresh_view()

    def _current_endpoint(self) -> str:
        return self.endpoint_combo.currentText().strip()

    def _current_model_id(self) -> int | None:
        return self.model_combo.currentData() if hasattr(self, "model_combo") else None

    def _fmt_value(self, value) -> str:
        """Categorical endpoints show whole-number class labels; continuous show 4 decimals."""
        if value is None:
            return ""
        if getattr(self, "_current_kind", "continuous") == "categorical":
            return str(int(round(float(value))))
        return f"{float(value):.4f}"

    def _set_row(self, r: int, mid: int, name: str, value, unit: str, subset: str = "") -> None:
        id_item = QTableWidgetItem(str(mid))
        id_item.setFlags(id_item.flags() & ~Qt.ItemIsEditable)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        subset_item = QTableWidgetItem(subset)
        subset_item.setFlags(subset_item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 0, id_item)
        self.table.setItem(r, 1, name_item)
        self.table.setItem(r, 2, QTableWidgetItem(self._fmt_value(value)))
        self.table.setItem(r, 3, QTableWidgetItem(unit or self.unit_combo.currentText().strip()))
        self.table.setItem(r, 4, subset_item)

    def refresh_view(self) -> None:
        if not hasattr(self, "table"):
            return
        from amdockvs.ui.tools.qsar.chart import histogram

        endpoint = self._current_endpoint() or None
        model_id = self._current_model_id()

        def work():
            # All heavy work off the GUI thread: resolve the endpoint (default to the first when
            # none is selected — never fetch every endpoint's rows), fetch its rows, AND bin the
            # histogram here so the GUI thread only renders ~10 bars + the rows.
            kinds = self.runtime.qsar.endpoint_kinds()
            resolved = endpoint or (sorted(kinds)[0] if kinds else None)
            rows = self.runtime.qsar.activity_rows(endpoint=resolved)
            models = self.runtime.qsar.list_models()
            subsets = self.runtime.qsar.model_subsets(model=model_id) if model_id else {}
            bins = histogram([r["value"] for r in rows if r["value"] is not None])
            return kinds, resolved, rows, models, subsets, bins

        run_async(
            work,
            self._fill,
            on_error=lambda exc: self.status.setText(str(exc)),
            busy=self.table,
        )

    def _fill(self, payload) -> None:
        kinds, resolved_endpoint, rows, models, subsets, bins = payload
        endpoints = sorted(kinds)
        self._loading = True
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        self.endpoint_combo.addItems(endpoints)
        if resolved_endpoint:
            self.endpoint_combo.setCurrentText(resolved_endpoint)
        self.endpoint_combo.blockSignals(False)
        self._current_kind = kinds.get(resolved_endpoint, "continuous")
        current_model = self._current_model_id()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("(none)", None)
        for m in models:
            self.model_combo.addItem(f"#{m.id} {m.name}", int(m.id))
        idx = self.model_combo.findData(current_model)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)
        # blockSignals: every setItem would otherwise fire itemChanged (N*5 emissions) and lag hard.
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._set_row(r, row["molecule_id"], row["name"], row["value"], row["unit"],
                          subsets.get(row["molecule_id"], ""))
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)
        self._loading = False
        n_test = sum(1 for v in subsets.values() if v == "test")
        suffix = f" — {n_test} test / {len(subsets) - n_test} train" if subsets else ""
        self.status.setText(f"{len(rows)} activity row(s) for endpoint '{self._current_endpoint() or '(all)'}'{suffix}.")
        if self._show_histogram is not None:
            self._show_histogram(self._current_endpoint(), bins)

    def _on_item_changed(self, item) -> None:
        if self._loading or item.column() not in (2, 3):
            return
        endpoint = self._current_endpoint()
        if not endpoint:
            QMessageBox.information(self, "Activities", "Set an endpoint first.")
            return
        try:
            ligand_id = int(self.table.item(item.row(), 0).text())
            value = float(self.table.item(item.row(), 2).text())
        except (TypeError, ValueError):
            return
        unit = (self.table.item(item.row(), 3).text() if self.table.item(item.row(), 3) else "").strip()
        try:
            self.runtime.qsar.set_activity(ligand_id=ligand_id, endpoint=endpoint, value=value, unit=unit)
        except Exception as exc:
            QMessageBox.warning(self, "Activities", str(exc))

    def _add_ligands(self) -> None:
        from amdockvs.ui.tools.qsar.activities import pick_ligands

        chosen = pick_ligands(self, self.runtime)
        if not chosen:
            return
        self._loading = True
        existing = {int(self.table.item(r, 0).text()) for r in range(self.table.rowCount())}
        for mid, name in chosen:
            if mid in existing:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._set_row(r, mid, name, None, "")
        self._loading = False
        self.status.setText("Enter a value in the Value column to record each activity.")

    def _load_csv(self) -> None:
        from amdockvs.ui.tools.qsar.activities import load_activities_dialog

        params = load_activities_dialog(self, current_endpoint=self._current_endpoint())
        if not params:
            return
        self.status.setText("Loading activities…")
        run_async(
            lambda: self.runtime.qsar.load_activities(**params),
            self._on_csv_loaded,
            on_error=lambda exc: self.status.setText(f"Load failed: {exc}"),
            busy=self.table,
        )

    def _on_csv_loaded(self, result: dict) -> None:
        self.endpoint_combo.setCurrentText(result["endpoint"])
        self.status.setText(
            f"Loaded {result['loaded']} activities (matched by {result['match_by']}); "
            f"skipped {result['skipped_missing_ligand']} unmatched, {result['skipped_invalid_value']} invalid."
        )
        self.refresh_view()

    def _load_matrix(self) -> None:
        from amdockvs.ui.tools.qsar.activities import map_activity_columns_dialog

        params = map_activity_columns_dialog(self)
        if not params:
            return
        self.status.setText("Loading activity columns…")
        run_async(
            lambda: self.runtime.qsar.load_activity_matrix(**params),
            self._on_matrix_loaded,
            on_error=lambda exc: self.status.setText(f"Load failed: {exc}"),
            busy=self.table,
        )

    def _on_matrix_loaded(self, result: dict) -> None:
        n_cat = sum(1 for k in result["kinds"].values() if k == "categorical")
        self.status.setText(
            f"Loaded {result['loaded']} activities across {len(result['endpoints'])} endpoints "
            f"({n_cat} categorical)."
        )
        if result["endpoints"]:
            self.endpoint_combo.setCurrentText(result["endpoints"][0])
        self.refresh_view()

    def _normalize(self) -> None:
        QMessageBox.information(
            self, "Normalize",
            "Use 'Load from CSV…' with a Transform set to pIC50 to normalize a concentration table. "
            "Manually-typed values are stored as-is in the chosen unit.",
        )

    def _delete_selected(self) -> None:
        endpoint = self._current_endpoint() or None
        ids = sorted({int(self.table.item(idx.row(), 0).text()) for idx in self.table.selectedIndexes()})
        if not ids:
            return
        for ligand_id in ids:
            try:
                self.runtime.qsar.delete_activity(ligand_id=ligand_id, endpoint=endpoint)
            except Exception as exc:
                QMessageBox.warning(self, "Activities", str(exc))
                break
        self.refresh_view()


class ComplexWidget(DockingResultsWidget):
    def __init__(self, *, runtime, load_hit_in_pymol: Callable[[DockingHitSummary, int], None] | None = None, parent=None):
        super().__init__(runtime=runtime, load_hit_in_pymol=load_hit_in_pymol, parent=parent)
