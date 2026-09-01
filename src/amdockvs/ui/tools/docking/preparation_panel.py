from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget, QGridLayout,
)
from amdockvs.ui.async_query import run_async
from amdockvs.ui.widgets import right_aligned, split_button
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID
from amdockvs.ui.catalog.binding_sites import BINDING_SITES_VIEW_ID
from amdockvs.ui.tools.molecules.build import BUILD_ID
from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.docking.protocols import PROTOCOL_SCHEMA, protocol_hash, protocol_identity
from amdockvs.docking.programs import GNINA_PROGRAM, VINA_PROGRAM, list_docking_programs
from amdockvs.models import EngineState
from amdockvs.vocab import MoleculeType
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
)
from ms_components.ms_stepper import Orientation, QStepper

DOCKING_VIEW_ID = "workspace.docking"
PREP_STATUS_VIEW_ID = "workspace.prep_status"
DEFAULT_PROGRAM = VINA_PROGRAM.key
MAX_REDOCKING_PROTOCOLS = 12
# Non-terminal job statuses — a docking job in any of these is "live" for duplicate detection.
_ACTIVE_JOB_STATUSES = ("pending", "running", "staging", "cancel_requested")

def _prep_error_message(files: object) -> str:
    if not isinstance(files, dict):
        return ""
    return str(files.get("error") or "")


def _engine_state_table_config(*, role_type: str) -> TableConfig:
    """Per-(molecule, engine) preparation status — the long/key-value `engines` table.

    `engine` and `is_ready` are real indexed columns, so they filter natively; no
    virtual columns or pivot needed. One row per molecule per engine.
    """
    return TableConfig(
        model_class=EngineState,
        columns=[
            ColumnDef("molecule_id", label="Molecule", width=90, sortable=False, filterable=True,
                      align=AlignHint.RIGHT),
            # Known engines derived from the program registry — no static list to fall out of sync.
            ColumnDef("engine", label="Engine", width=120, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE,
                      choices=tuple(sorted({spec.docking_engine for spec in list_docking_programs()}))),
            ColumnDef(
                "is_ready",
                label="Prepared",
                width=90,
                # sortable=True,
                filterable=True,
                align=AlignHint.CENTER,
                formatter=lambda value: "✓" if bool(value) else "✗",
            ),
            ColumnDef(
                "files",
                label="Message",
                width=360,
                # sortable=False,
                filterable=False,
                formatter=lambda value: _prep_error_message(value) or "—",
                tooltip=lambda row: row.get("files") if row.get("files") != "—" else None,
            ),
        ],
        default_filters=[FilterSpec("role_type", FilterOperator.EQ, role_type, label="role")],
        default_sort=[SortSpec("is_ready"), SortSpec("updated_at", descending=True)],
        page_size=20,
        page_size_options=[10, 20, 50, 100],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        multi_select=False,
        empty_message="No preparation records yet for this role",
    )


# Ligand and receptor prep status were two registered views over the same `engines` table,
# one filter value apart. They are one view with a role selector instead.
_PREP_ROLES = (("Ligands", "ligand"), ("Receptors", "receptor"))


class EngineStatePrepView(BoundTableWidget):
    """Prepared/not per (molecule, engine), from the `engines` table. The role combo
    swaps the base filter in place — same table, same columns, one population at a time."""

    def __init__(self, *, runtime, role_type: str = "ligand", parent=None):
        super().__init__(
            runtime=runtime,
            config=_engine_state_table_config(role_type=role_type),
            empty_text="Open or create a project to inspect preparation status.",
            parent=parent,
        )
        if self._table is None:  # no active project: nothing to filter
            return
        self._role_selector = QComboBox()
        for label, value in _PREP_ROLES:
            self._role_selector.addItem(label, value)
        self._role_selector.setCurrentIndex(max(0, self._role_selector.findData(role_type)))
        self._role_selector.currentIndexChanged.connect(lambda _index: self._apply_role())
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 2)
        row.addWidget(QLabel("Role:"))
        row.addWidget(self._role_selector)
        row.addStretch(1)
        self.layout().insertLayout(0, row)

    def _apply_role(self) -> None:
        role = str(self._role_selector.currentData())
        self.set_base_filter(
            "role_type", FilterSpec("role_type", FilterOperator.EQ, role, label="role")
        )



def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


class _WheelGuard(QObject):
    """Swallow wheel events on combos/spinboxes unless they have focus.

    Inside a scroll area, the wheel otherwise changes the value under the cursor
    instead of scrolling the page. With StrongFocus + this filter, the widget only
    reacts to the wheel after you click into it; otherwise the wheel scrolls.
    """

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            return True
        return super().eventFilter(obj, event)




class PreparationPanel:
    """Ligand/receptor preparation component and preparation-status view."""

    def _build_ligands_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        # ONE scope selector (no separate source + prepare-scope combos, which could
        # contradict each other). Each mode resolves to a transient MoleculeScope used for
        # BOTH preparation and docking. Selected/Filtered are constrained by experiment:
        # docking uses general ligands, redocking uses reference ligands.

        # No ligand table here: the catalog Ligands tab IS the table, kept in sync with this
        # scope through _sync_ligand_table_filter. Per-engine prep status is its own tab
        # (PREP_STATUS_VIEW_ID) — only the counts stay, on the scope line below.
        row = QHBoxLayout()
        row.addWidget(QLabel("Scope", page))
        self.ligand_scope_combo = QComboBox(page)
        # Only as wide as its longest entry.
        self.ligand_scope_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._sync_ligand_scope_options()
        self.ligand_scope_combo.currentIndexChanged.connect(self._on_ligand_mode_changed)
        row.addWidget(self.ligand_scope_combo)
        # row.addStretch(1)
        layout.addLayout(row)

        # Counts ("N ligand(s) · M prepared · K failed") on their own line under the selector,
        # so a long status never squeezes the combo.
        self.ligand_scope_label = QLabel("Ligand scope unresolved.", page)
        self.ligand_scope_label.setWordWrap(True)
        layout.addWidget(self.ligand_scope_label)

        # Preparation area (image-3 design): a vertical target list on the left drives a
        # QStackedWidget of option pages — row 0 = General Options (orchestration: batch,
        # force re-prepare), then ONE page per preparation *family*, not per program: Vina,
        # gnina and AutoDock4 all write a single EngineState row with engine="ad4", so
        # listing them separately showed three targets for one job. Families not implied by
        # the Programs step are disabled in the list.
        prep_box = QGroupBox("Ligand preparation", page)
        prep_h = QHBoxLayout(prep_box)

        self.prep_target_list = QListWidget(prep_box)
        self.prep_target_list.setMaximumWidth(170)
        self.prep_stack = QStackedWidget(prep_box)

        general_item = QListWidgetItem("General Options")
        general_item.setData(Qt.UserRole, None)
        self.prep_target_list.addItem(general_item)
        self.prep_stack.addWidget(self._build_general_prep_page())
        for engine, specs in self._prep_families().items():
            item = QListWidgetItem(self._prep_family_label(engine, specs))
            # UserRole keeps a representative *program* key: prepare_ligands() takes a program
            # and resolves the engine itself, so the launch path is unchanged.
            item.setData(Qt.UserRole, specs[0].key)
            item.setData(Qt.UserRole + 1, engine)
            item.setToolTip("Shared by: " + ", ".join(spec.label for spec in specs))
            self.prep_target_list.addItem(item)
            self.prep_stack.addWidget(self._build_family_prep_page(engine, specs))
        self.prep_target_list.setCurrentRow(0)
        self.prep_target_list.currentRowChanged.connect(self.prep_stack.setCurrentIndex)
        prep_h.addWidget(self.prep_target_list)
        prep_h.addWidget(self.prep_stack, 1)

        # Floor for the options pane: its pages are scroll areas (no real minimum), so without
        # this the layout can crush the box until the controls are unreachable.
        prep_box.setMinimumHeight(200)
        layout.addWidget(prep_box, 1)

        # The launch button sits OUTSIDE the options group, bottom-right: it acts on the whole
        # step, not on the option page that happens to be selected, and inside the group box the
        # layout stretched it to the full height of the pane.
        self.prepare_ligands_button = split_button(
            "Prepare Ligands", page, on_click=self._prepare_ligands, primary=True
        )
        self.prepare_ligands_button.menu().addAction(
            "Save to workflow…", self._save_prepare_ligands_to_workflow
        ).setToolTip("Add 'Prepare ligands' (current settings) as a workflow step — updates it if already there.")
        layout.addLayout(right_aligned(self.prepare_ligands_button))
        return page

    def _prep_families(self) -> dict[str, list]:
        """preparation_engine -> the programs that share it, for programs needing ligand prep.

        The engine is what EngineState is keyed by, so it — not the program — is the unit of
        preparation, of status counting and of the "hide already prepared" filter.
        """
        families: dict[str, list] = {}
        for spec in list_docking_programs():
            if not spec.requires_ligand_preparation:
                continue
            families.setdefault(str(spec.preparation_engine), []).append(spec)
        return families

    @staticmethod
    def _prep_family_label(engine: str, specs: list) -> str:
        if len(specs) == 1:
            return str(specs[0].label)
        return f"{engine.upper()} family"

    def _refresh_prep_targets(self) -> None:
        # Enable a family when ANY of the programs sharing its engine is selected; hide it
        # when none of them is even available for this experiment configuration.
        if not hasattr(self, "prep_target_list"):
            return
        selected = set(self._selected_programs())
        available = {spec.key for spec in self._available_program_specs()}
        families = self._prep_families()
        for index in range(self.prep_target_list.count()):
            item = self.prep_target_list.item(index)
            engine = item.data(Qt.UserRole + 1)
            keys = {spec.key for spec in families.get(engine, [])} if engine else set()
            if engine is not None:
                item.setHidden(not (keys & available))
            enabled = engine is None or bool(keys & selected & available)
            flags = item.flags()
            item.setFlags(flags | Qt.ItemIsEnabled if enabled else flags & ~Qt.ItemIsEnabled)
        current = self.prep_target_list.currentItem()
        if current is None or current.isHidden() or not (current.flags() & Qt.ItemIsEnabled):
            self.prep_target_list.setCurrentRow(0)

    def _selected_prep_engines(self) -> list[str]:
        """Distinct preparation engines implied by the selected programs (usually just one)."""
        selected = set(self._selected_programs())
        return [
            engine
            for engine, specs in self._prep_families().items()
            if selected & {spec.key for spec in specs}
        ]

    def _set_prep_family_counts(self, by_engine: dict[str, int], total: int) -> None:
        # "N / total" per family, right on the row that selects it — one scope, K statuses.
        families = self._prep_families()
        for index in range(self.prep_target_list.count()):
            item = self.prep_target_list.item(index)
            engine = item.data(Qt.UserRole + 1)
            if engine is None:
                continue
            name = self._prep_family_label(engine, families.get(engine, []))
            prepared = by_engine.get(engine)
            item.setText(name if prepared is None else f"{name}\n{prepared} / {total}")

    def _selected_prep_target(self) -> str | None:
        item = self.prep_target_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _build_general_prep_page(self) -> QWidget:
        # Options common to every preparation method.
        page = QWidget(self)
        form = QFormLayout(page)
        self.prepare_ligand_batch_size = _spinbox(minimum=1, maximum=2048, value=64)
        self.force_prepare_ligands = QCheckBox("Force re-prepare", page)
        # The Ligands table shows exactly what this step will process, so it follows this box.
        self.force_prepare_ligands.toggled.connect(lambda _=False: self._sync_ligand_table_filter())
        form.addRow("Prep batch", self.prepare_ligand_batch_size)
        form.addRow(self.force_prepare_ligands)
        return self._in_scroll(page)

    def _build_family_prep_page(self, engine: str, specs: list) -> QWidget:
        # Family-specific prep options (Meeko / AutoDockTools for the AD4 family). Placeholder
        # until the prep backend accepts per-engine options; uniform QFormLayout so every
        # page matches.
        page = QWidget(self)
        form = QFormLayout(page)
        note = QLabel(
            f"Ligand preparation for engine «{engine}», shared by "
            f"{', '.join(spec.label for spec in specs)}.\n"
            "Meeko / AutoDockTools options will be configurable here; engine defaults for now.",
            page,
        )
        note.setWordWrap(True)
        form.addRow(note)
        return self._in_scroll(page)

    def _build_receptors_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        # Scope: the counts + a collapsible context panel (Flexible residues) for the receptor
        # focused in the catalog table.
        row = QHBoxLayout()
        row.addWidget(QLabel("Scope", page))
        self.receptor_scope_combo = QComboBox(page)
        self.receptor_scope_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)


        self.receptor_scope_combo.addItem("Active (all receptors)", "active")
        self.receptor_scope_combo.addItem("Selected (marked in table)", "selected")
        self.receptor_scope_combo.addItem("Filtered (all matching table filter)", "filtered")

        self.receptor_scope_combo.currentIndexChanged.connect(self._on_receptor_mode_changed)
        row.addWidget(self.receptor_scope_combo)
        # row.addStretch(1)
        layout.addLayout(row)

        # Status text on the scope row instead of a line of its own (same as the Ligands step).
        self.receptor_scope_label = QLabel("Receptor scope unresolved.", page)
        self.receptor_scope_label.setWordWrap(True)
        layout.addWidget(self.receptor_scope_label)

        # No receptor table here either: the catalog Receptors tab IS the table (same deal as
        # the Ligands step), kept in sync by _sync_receptor_table_filter. Flexible residues
        # still follow the row you click there.
        self.receptor_side_panel = self._build_receptor_side_panel(page)
        layout.addWidget(self.receptor_side_panel, 1)

        # Preparation comes AFTER the binding site: flexible residues chosen above feed into the
        # prepared receptor. Vertical target list (General Options + one page per program that
        # requires receptor prep) driving a stacked widget, plus the big Prepare button.
        prep_box = QGroupBox("Receptor preparation", page)
        prep_h = QHBoxLayout(prep_box)

        self.receptor_prep_target_list = QListWidget(prep_box)
        self.receptor_prep_target_list.setMaximumWidth(170)
        self.receptor_prep_stack = QStackedWidget(prep_box)

        general_item = QListWidgetItem("General Options")
        general_item.setData(Qt.UserRole, None)
        self.receptor_prep_target_list.addItem(general_item)
        self.receptor_prep_stack.addWidget(self._build_receptor_general_prep_page())
        for spec in list_docking_programs():
            if not spec.requires_receptor_preparation:
                continue
            item = QListWidgetItem(spec.label)
            item.setData(Qt.UserRole, spec.key)
            self.receptor_prep_target_list.addItem(item)
            self.receptor_prep_stack.addWidget(self._build_receptor_program_prep_page(spec))
        self.receptor_prep_target_list.setCurrentRow(0)
        self.receptor_prep_target_list.currentRowChanged.connect(self.receptor_prep_stack.setCurrentIndex)
        prep_h.addWidget(self.receptor_prep_target_list)
        prep_h.addWidget(self.receptor_prep_stack, 1)

        # Splitter instead of stacked boxes: the step then fits any height on its own, which
        # is what lets it drop the scroll area (and its scrollbar inside the table's).
        prep_box.setMinimumHeight(200)  # same reason as the Ligands step

        layout.addWidget(prep_box, 1)
        # Outside the group, bottom-right — same reason as the Ligands step.
        self.prepare_receptor_button = split_button(
            "Prepare Receptors", page, on_click=self._prepare_receptors, primary=True
        )
        self.prepare_receptor_button.menu().addAction(
            "Save to workflow…", self._save_prepare_receptors_to_workflow
        ).setToolTip("Add 'Prepare receptors' (current settings) as a workflow step — updates it if already there.")
        layout.addLayout(right_aligned(self.prepare_receptor_button))
        return page

    def _build_receptor_side_panel(self, parent: QWidget) -> QWidget:
        # Collapsible context panel (mockup's green column) for the focused receptor.
        panel = QWidget(parent)
        panel.setObjectName("recSidePanel")
        # panel.setStyleSheet(
        #     "#recSidePanel { background:#16291f; border:1px solid #3a7d5a; border-radius:8px; }"
        # )
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        # No "Active Site" box: which BS is active and its center/size are already on the
        # PyMOL Grid Box panel. The only number not shown there (how many receptors have a
        # grid) rides along on the scope line, and Binding Sites opens from the scope row.

        # Flexible residues only make sense once the receptor has an active site → disabled
        # until the focused receptor has a grid.
        self.flex_box = self._build_flex_residues_box(panel)
        self.flex_box.setEnabled(False)
        v.addWidget(self.flex_box, 1)
        return panel

    def _build_receptor_general_prep_page(self) -> QWidget:
        # Options common to every receptor preparation method.
        page = QWidget(self)
        form = QFormLayout(page)
        self.prepare_receptor_batch_size = _spinbox(minimum=1, maximum=256, value=8)
        self.force_prepare_receptors = QCheckBox("Force re-prepare", page)
        # The Receptors table shows exactly what this step will process, so it follows this box.
        self.force_prepare_receptors.toggled.connect(lambda _=False: self._sync_receptor_table_filter())
        # What import kept (structural waters, cofactors, coordination metals) is in the receptor
        # file; these decide what reaches the PDBQT. Metals always go in — they are part of the
        # site, and Vina types them. The prepared file has one name per receptor+engine, so
        # changing these on an already-prepared receptor needs "Force re-prepare".
        self.keep_waters_receptors = QCheckBox("Include structural waters", page)
        self.keep_waters_receptors.setToolTip(
            "Dock against the structural waters kept at import (Meeko writes them as OA/HD with "
            "TIP3P charges). Off = dry receptor, the usual default."
        )
        self.keep_cofactors_receptors = QCheckBox("Include cofactors", page)
        self.keep_cofactors_receptors.setToolTip(
            "Keep cofactors (HEM, NAD, FAD, ...) in the receptor PDBQT. Off leaves an empty "
            "cofactor pocket the ligand can dock into."
        )
        form.addRow("Prep batch", self.prepare_receptor_batch_size)
        form.addRow(self.force_prepare_receptors)
        form.addRow(self.keep_waters_receptors)
        form.addRow(self.keep_cofactors_receptors)
        return self._in_scroll(page)

    def _build_receptor_program_prep_page(self, spec) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        note = QLabel(
            f"{spec.label} receptor preparation (engine: {spec.preparation_engine}).\n"
            "AutoDockTools options will be configurable here; engine defaults for now.",
            page,
        )
        note.setWordWrap(True)
        form.addRow(note)
        return self._in_scroll(page)

    def _on_receptor_mode_changed(self) -> None:
        self.refresh()

    def _selected_receptor_prep_target(self) -> str | None:
        item = self.receptor_prep_target_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _refresh_receptor_prep_targets(self) -> None:
        if not hasattr(self, "receptor_prep_target_list"):
            return
        selected = set(self._selected_programs())
        available = {spec.key for spec in self._available_program_specs()}
        for index in range(self.receptor_prep_target_list.count()):
            item = self.receptor_prep_target_list.item(index)
            key = item.data(Qt.UserRole)
            if key is not None and hasattr(item, "setHidden"):
                item.setHidden(key not in available)
            enabled = key is None or (key in selected and key in available)
            flags = item.flags()
            item.setFlags(flags | Qt.ItemIsEnabled if enabled else flags & ~Qt.ItemIsEnabled)
        current = self.receptor_prep_target_list.currentItem()
        if current is None or current.isHidden() or not (current.flags() & Qt.ItemIsEnabled):
            self.receptor_prep_target_list.setCurrentRow(0)

    def _count_failed_preparations(self, scope, *, role_type: str, engine: str = "ad4") -> int:
        return self.readiness_service.preparation(
            scope,
            role_type=role_type,
            engine=engine,
        ).failed

    def _catalog_ligand_widget(self):
        """The catalog Ligands tab, if it is open — this step's ligand table.

        Only ``open_view`` (never open_or_focus_view): syncing runs on every refresh and must
        not pop a tab the user closed.
        """
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(LIGANDS_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break refresh
            return None

    def _scope_usage_class(self) -> str:
        mode = self._ligand_scope_mode()
        if mode in {"general", "reference"}:
            return mode
        return "reference" if self._run_kind() == "redocking" else "general"

    def _sync_ligand_table_filter(self) -> None:
        """Push the experiment combination (ligand type + usage class) onto the catalog
        Ligands table as this step's scope, so its rows are exactly what the step works on
        and the table total matches the scope count.

        These override the user's own Usage/Type column filters while the step is mounted —
        base filters share the field-keyed store — and pop_scope restores the catalog's
        defaults when the step is hidden.
        """
        widget = self._catalog_ligand_widget()
        if widget is None:
            return
        usage_class = self._scope_usage_class()
        clause = self._unprepared_clause("ligand")
        widget.push_scope(
            self._SCOPE_KEY,
            filters=[
                FilterSpec("molecule_type", FilterOperator.EQ, self._ligand_type(), label="ligand_type"),
                FilterSpec("usage_class", FilterOperator.EQ, usage_class, label=usage_class),
            ],
            clause=clause,
            # With the clause on, an empty table means "nothing left to prepare", not "no
            # ligands imported" — and Import Ligands… doesn't fix it.
            empty_message="Every ligand in this scope is already prepared" if clause is not None else None,
            show_action=clause is None,
        )
        self._bind_ligand_table_signals(widget)

    # This tool's scope key on any table it borrows (see BoundTableWidget.push_scope).
    _SCOPE_KEY = "docking"

    # Which step owns each role's table filter — the clause is only applied while that step
    # is the current one; the other steps work on the whole scope.
    _PREP_STEP = {"ligand": 1, "receptor": 2}

    def _unprepared_clause(self, role: str):
        """On its own step the table shows what preparation will actually touch: with
        `Force re-prepare` off that's only the rows still missing preparation, so the table
        agrees with the "N / M prepared" counters.

        One NOT EXISTS per family, OR-ed: a row is worth showing while ANY selected family
        still lacks it. With K=1 (today) that's a single anti-join on the unique
        (molecule_id, role_type, engine) index — ~1 index probe per candidate row.
        """
        force = getattr(self, f"force_prepare_{role}s", None)
        if force is None or force.isChecked() or self.stepper.current_index != self._PREP_STEP[role]:
            return None
        engines = self._selected_prep_engines()
        if not engines:
            return None
        return self.runtime.docking.unprepared_molecules_clause(
            role_type=role,
            engines=engines,
        )

    def _bind_ligand_table_signals(self, widget) -> None:
        # The catalog tab outlives this step and can be closed/reopened, so bind per widget
        # instance and only once.
        table = getattr(widget, "table", None)
        if table is None or table is self._bound_ligand_table:
            return
        table.selection_changed.connect(self._on_ligand_selection_changed)
        table.refresh_clicked.connect(self.refresh)
        self._bound_ligand_table = table

    def _release_ligand_table(self) -> None:
        """Hand the catalog Ligands table back, so closing the step doesn't leave it
        silently filtered."""
        widget = self._catalog_ligand_widget()
        if widget is not None:
            widget.pop_scope(self._SCOPE_KEY)

    def _catalog_receptor_widget(self):
        """The catalog Receptors tab, if it is open — this step's receptor table."""
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(RECEPTOR_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break refresh
            return None

    def _sync_receptor_table_filter(self) -> None:
        """Same contract as _sync_ligand_table_filter, on the catalog Receptors tab."""
        widget = self._catalog_receptor_widget()
        if widget is None:
            return
        clause = self._unprepared_clause("receptor")
        widget.push_scope(
            self._SCOPE_KEY,
            filters=[FilterSpec("molecule_type", FilterOperator.EQ, self._receptor_type(),
                                label="receptor_type")],
            clause=clause,
            empty_message="Every receptor in this scope is already prepared" if clause is not None else None,
            show_action=clause is None,
        )
        self._bind_receptor_table_signals(widget)

    def _bind_receptor_table_signals(self, widget) -> None:
        # Flexible residues follow the row clicked in the catalog table, exactly as they did
        # with the table this step used to embed.
        table = getattr(widget, "table", None)
        if table is None or table is self._bound_receptor_table:
            return
        table.row_clicked.connect(self._on_receptor_clicked)
        table.selection_changed.connect(self._on_receptor_selection_changed)
        table.refresh_clicked.connect(self.refresh)
        self._bound_receptor_table = table

    def _release_receptor_table(self) -> None:
        self._reset_receptor_focus()
        widget = self._catalog_receptor_widget()
        if widget is not None:
            widget.pop_scope(self._SCOPE_KEY)

    def _reset_receptor_focus(self) -> None:
        """Leaving the step drops the focused receptor: its binding site and the residues
        loaded from its box belong to that receptor, and showing them next to a different one
        (or none) is worse than showing nothing."""
        self._focused_receptor_id = None
        self._clear_flex_panel()
        self.flex_box.setEnabled(False)

    def _on_ligand_mode_changed(self) -> None:
        self._sync_ligand_table_filter()
        # Selected/Filtered are read off the catalog table, so it has to be on screen.
        if self._ligand_scope_mode() in ("selected", "filtered"):
            opener = getattr(self.window(), "open_or_focus_view", None)
            if callable(opener):
                opener(LIGANDS_VIEW_ID)
        self.refresh()

    def _on_ligand_selection_changed(self, objects: list[object]) -> None:
        ids = sorted(
            {int(getattr(o, "id", 0) or 0) for o in list(objects or []) if int(getattr(o, "id", 0) or 0) > 0}
        )
        # Ignore empty (scroll/prepare clears the table selection, not the user's intent).
        if ids:
            self._selected_ligand_ids = ids[: self._SELECTION_CAP]
        if self._ligand_scope_mode() == "selected":
            self._req_preview_timer.start()

    def _prepare_ligands(self) -> None:
        ligand_scope = self._prep_ligand_scope()
        # The prep-target list picks scope: "General" → all selected engines, else just one.
        target = self._selected_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        job_ids: dict[str, str] = {}
        try:
            for program in programs:
                job_ids[program] = self.runtime.docking.prepare_ligands(
                    program=program,
                    ligand_set=ligand_scope,
                    batch_size=max(1, int(self.prepare_ligand_batch_size.value())),
                    force=self.force_prepare_ligands.isChecked(),
                    executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
                )
        except Exception as exc:
            self._error("Prepare Ligands", exc)
            return
        self._append_status("Ligand Preparation Submitted", {"job_ids": job_ids})
        # The job is queued and the step is done: reset the scope to its default (a stale
        # "Selected" would silently narrow the run too) and move on to Receptors.
        self.ligand_scope_combo.setCurrentIndex(0)
        self.stepper.set_current_index(2)

    def _prepare_receptors(self) -> None:
        job_ids: dict[str, str] = {}
        target = self._selected_receptor_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        try:
            receptor_scope = self._selected_receptor_scope()
            for program in programs:
                job_ids[program] = self.runtime.docking.prepare_receptors(
                    program=program,
                    receptor_set=receptor_scope,
                    batch_size=max(1, int(self.prepare_receptor_batch_size.value())),
                    force=self.force_prepare_receptors.isChecked(),
                    keep_waters=self.keep_waters_receptors.isChecked(),
                    keep_cofactors=self.keep_cofactors_receptors.isChecked(),
                    executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
                )
        except ValueError as exc:
            self._warn("Prepare Receptor", str(exc))
            return
        except Exception as exc:
            self._error("Prepare Receptor", exc)
            return
        self._append_status("Receptor Preparation Submitted", {"job_ids": job_ids, "receptor_ids": self._effective_receptor_ids()})

    def _save_prepare_ligands_to_workflow(self) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        # Capture current config now (widget-free submit). check_required=False: in a workflow this
        # waits for the 3D step, so the submit-time has_3d gate would always fail.
        scope = self._prep_ligand_scope()
        target = self._selected_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        batch = max(1, int(self.prepare_ligand_batch_size.value()))
        force = self.force_prepare_ligands.isChecked()
        executor = DEFAULT_LOCAL_CPU_EXECUTOR

        def submit(rt, programs=programs, scope=scope, batch=batch, force=force, executor=executor):
            return [
                rt.docking.prepare_ligands(
                    program=p, ligand_set=scope, batch_size=batch, force=force,
                    executor_name=executor, check_required=False,
                )
                for p in programs
            ]

        save_to_workflow(self.window(), kind="prepare_ligands", name="Prepare ligands", category="prepare", submit=submit)

    def _save_prepare_receptors_to_workflow(self) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        scope = self._selected_receptor_scope()
        target = self._selected_receptor_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        batch = max(1, int(self.prepare_receptor_batch_size.value()))
        force = self.force_prepare_receptors.isChecked()
        waters = self.keep_waters_receptors.isChecked()
        cofactors = self.keep_cofactors_receptors.isChecked()
        executor = DEFAULT_LOCAL_CPU_EXECUTOR

        def submit(rt, programs=programs, scope=scope, batch=batch, force=force, executor=executor,
                   waters=waters, cofactors=cofactors):
            return [
                rt.docking.prepare_receptors(
                    program=p, receptor_set=scope, batch_size=batch, force=force, executor_name=executor,
                    keep_waters=waters, keep_cofactors=cofactors,
                )
                for p in programs
            ]

        save_to_workflow(self.window(), kind="prepare_receptors", name="Prepare receptors", category="prepare", submit=submit)
