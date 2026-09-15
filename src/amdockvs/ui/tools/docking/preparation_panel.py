from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QStackedWidget,
    QWidget,
)
from amdockvs.ui.common.job_follower import JobFollower
from amdockvs.ui.common.widgets import RunDestinationCombo, gate_workflow_save, split_button
from ms_components.tool_panel import ToolPanel
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID
from amdockvs.docking.engines.programs import list_docking_programs
from amdockvs.models import EngineState
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

PREP_STATUS_VIEW_ID = "workspace.prep_status"

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
class EngineStatePrepView(BoundTableWidget):
    """Prepared/not per (molecule, engine), from the `engines` table. The Docking Studio step
    picks the role (`set_role`) — same table, same columns, one population at a time."""

    def __init__(self, *, runtime, role_type: str = "ligand", parent=None):
        super().__init__(
            runtime=runtime,
            config=_engine_state_table_config(role_type=role_type),
            empty_text="Open or create a project to inspect preparation status.",
            parent=parent,
        )
        self.role = role_type

    def set_role(self, role: str) -> None:
        if role == self.role or self._table is None:  # no active project: nothing to filter
            return
        self.role = role
        self.set_base_filter(
            "role_type", FilterSpec("role_type", FilterOperator.EQ, role, label="role")
        )



def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


class PreparationPanel:
    """Ligand/receptor preparation component and preparation-status view."""

    def _build_ligands_tab(self) -> QWidget:
        # No scope selector and no ligand table here: the catalog Ligands tab IS both. This
        # step prepares exactly the rows that table is showing, and _sync_ligand_table_filter
        # keeps the experiment combination (type + usage class) on it. Per-engine prep status
        # is its own tab (PREP_STATUS_VIEW_ID) — only the counts stay, on the action bar.
        panel = ToolPanel(self)

        # Preparation area (image-3 design): a vertical target list on the left drives a
        # QStackedWidget of option pages — row 0 = General Options (force re-prepare), then
        # ONE page per preparation *family*, not per program: Vina,
        # gnina and AutoDock4 all write a single EngineState row with engine="ad4", so
        # listing them separately showed three targets for one job. Families not implied by
        # the Programs step are disabled in the list.
        prep_section = panel.add_section("Ligand preparation")
        prep_box = QWidget(prep_section.body)
        prep_h = QHBoxLayout(prep_box)
        prep_h.setContentsMargins(0, 0, 0, 0)

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
        prep_section.add_row(prep_box)

        advanced = panel.advanced
        self.prepare_ligand_batch_size = _spinbox(minimum=1, maximum=2048, value=64)
        self.prepare_ligand_batch_label = QLabel("Batch size", advanced)
        advanced.add_row(self.prepare_ligand_batch_label, self.prepare_ligand_batch_size)
        self.ligand_prep_destination = RunDestinationCombo(advanced, self.runtime)
        advanced.add_row("Run on", self.ligand_prep_destination)
        advanced.form.setRowVisible(self.ligand_prep_destination, self.ligand_prep_destination.count() > 1)

        # The launch button acts on the whole step, not on the option page that happens to be
        # selected, so it lives on the action bar.
        self.ligand_bar = panel.action_bar
        self.prepare_ligands_button = split_button("Prepare Ligands", self.ligand_bar, on_click=self._prepare_ligands)
        gate_workflow_save(self.prepare_ligands_button.menu().addAction(
            "Save to workflow…", self._save_prepare_ligands_to_workflow
        ))
        self.ligand_bar.add_action(self.prepare_ligands_button)
        self.ligand_bar.set_summary("Ligand scope unresolved.")
        self.ligand_prep_jobs = JobFollower(self, self.ligand_bar, noun="Ligand preparation")
        return panel

    def _prep_families(self, role: str = "ligand") -> dict[str, list]:
        """preparation_engine -> the programs that share it, for programs needing ligand prep.

        The engine is what EngineState is keyed by, so it — not the program — is the unit of
        preparation, of status counting and of the "hide already prepared" filter.
        """
        families: dict[str, list] = {}
        for spec in list_docking_programs():
            if not bool(getattr(spec, f"requires_{role}_preparation")):
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
        # Batch size and Run on live in the step's Advanced section.
        page = QWidget(self)
        form = QFormLayout(page)
        self.force_prepare_ligands = QCheckBox("Force re-prepare", page)
        # The Ligands table shows exactly what this step will process, so it follows this box.
        self.force_prepare_ligands.toggled.connect(lambda _=False: self._sync_ligand_table_filter())
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
        # No receptor table here either: the catalog Receptors tab IS the table (same deal as
        # the Ligands step), kept in sync by _sync_receptor_table_filter. Flexible residues
        # follow the row you click there; the scope counts ride on the action bar.
        panel = ToolPanel(self)

        # Flexible residues only make sense once the focused receptor has an active site, so
        # the section stays disabled until it does. No "Active Site" box: which BS is active
        # and its center/size are already on the PyMOL Grid Box panel.
        self.flex_box = panel.add_section(
            "Flexible residues", checkable=True, checked=False, collapse_when_unchecked=True
        )
        self.flex_box.add_row(self._build_flex_residues_box(self.flex_box.body))
        self.flex_box.setEnabled(False)

        # Preparation comes AFTER the binding site: flexible residues chosen above feed into the
        # prepared receptor. Vertical target list (General Options + one page per program that
        # requires receptor prep) driving a stacked widget.
        prep_section = panel.add_section("Receptor preparation")
        prep_box = QWidget(prep_section.body)
        prep_h = QHBoxLayout(prep_box)
        prep_h.setContentsMargins(0, 0, 0, 0)

        self.receptor_prep_target_list = QListWidget(prep_box)
        self.receptor_prep_target_list.setMaximumWidth(170)
        self.receptor_prep_stack = QStackedWidget(prep_box)

        general_item = QListWidgetItem("General Options")
        general_item.setData(Qt.UserRole, None)
        self.receptor_prep_target_list.addItem(general_item)
        self.receptor_prep_stack.addWidget(self._build_receptor_general_prep_page())
        for engine, specs in self._prep_families("receptor").items():
            item = QListWidgetItem(self._prep_family_label(engine, specs))
            item.setData(Qt.UserRole, specs[0].key)
            item.setData(Qt.UserRole + 1, engine)
            item.setToolTip("Shared by: " + ", ".join(spec.label for spec in specs))
            self.receptor_prep_target_list.addItem(item)
            self.receptor_prep_stack.addWidget(self._build_receptor_program_prep_page(specs[0]))
        self.receptor_prep_target_list.setCurrentRow(0)
        self.receptor_prep_target_list.currentRowChanged.connect(self.receptor_prep_stack.setCurrentIndex)
        prep_h.addWidget(self.receptor_prep_target_list)
        prep_h.addWidget(self.receptor_prep_stack, 1)

        # Splitter instead of stacked boxes: the step then fits any height on its own, which
        # is what lets it drop the scroll area (and its scrollbar inside the table's).
        prep_box.setMinimumHeight(200)  # same reason as the Ligands step
        prep_section.add_row(prep_box)

        advanced = panel.advanced
        self.prepare_receptor_batch_size = _spinbox(minimum=1, maximum=256, value=8)
        advanced.add_row("Batch size", self.prepare_receptor_batch_size)
        self.receptor_prep_destination = RunDestinationCombo(advanced, self.runtime)
        advanced.add_row("Run on", self.receptor_prep_destination)
        advanced.form.setRowVisible(self.receptor_prep_destination, self.receptor_prep_destination.count() > 1)

        # On the action bar — same reason as the Ligands step.
        self.receptor_bar = panel.action_bar
        self.prepare_receptor_button = split_button(
            "Prepare Receptors", self.receptor_bar, on_click=self._prepare_receptors
        )
        gate_workflow_save(self.prepare_receptor_button.menu().addAction(
            "Save to workflow…", self._save_prepare_receptors_to_workflow
        ))
        self.receptor_bar.add_action(self.prepare_receptor_button)
        self.receptor_bar.set_summary("Receptor scope unresolved.")
        self.receptor_prep_jobs = JobFollower(self, self.receptor_bar, noun="Receptor preparation")
        return panel

    def _build_receptor_general_prep_page(self) -> QWidget:
        # Options common to every receptor preparation method.
        # Batch size and Run on live in the step's Advanced section.
        page = QWidget(self)
        form = QFormLayout(page)
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

    def _selected_receptor_prep_target(self) -> str | None:
        item = self.receptor_prep_target_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _refresh_receptor_prep_targets(self) -> None:
        if not hasattr(self, "receptor_prep_target_list"):
            return
        selected = set(self._selected_programs())
        available = {spec.key for spec in self._available_program_specs()}
        families = self._prep_families("receptor")
        for index in range(self.receptor_prep_target_list.count()):
            item = self.receptor_prep_target_list.item(index)
            key = item.data(Qt.UserRole)
            engine = item.data(Qt.UserRole + 1)
            keys = {spec.key for spec in families.get(engine, [])} if engine else set()
            if key is not None and hasattr(item, "setHidden"):
                item.setHidden(not (keys & available))
            enabled = key is None or bool(keys & selected & available)
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
        """The catalog tab holding this step's ligands, if it is open.

        Which tab that is depends on the experiment and on where the library lives — see
        `_ligand_view_id`. Only ``open_view`` (never open_or_focus_view): syncing runs on
        every refresh and must not pop a tab the user closed.
        """
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(self._ligand_view_id())
        except Exception:  # noqa: BLE001 - a missing/failed view must not break refresh
            return None

    def _scope_usage_class(self) -> str:
        return self._ligand_usage_class(self._run_kind())

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
        self._set_sharded_prep_mode(self._ligand_scope_is_sharded())
        if self._ligand_scope_is_sharded():
            # The shards table is the scope, whole: there is no molecule column to narrow on
            # and no per-molecule preparation to hide rows for. Free the Ligands table on the
            # way out — switching away from redocking is how we get here.
            self._release_ligand_table()
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
        silently filtered. By view id, not by `_ligand_view_id()`: the run kind may have
        changed since the scope was pushed, and the table that holds it is the one to free."""
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return
        try:
            widget = central.open_view(LIGANDS_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break teardown
            return
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

    def _on_ligand_selection_changed(self, objects: list[object]) -> None:
        # Highlighting rows does not change the scope - narrowing the table does (right-click > Select).
        # This only keeps the preview honest while the user works in the table.
        self._req_preview_timer.start()

    def _prepare_ligands(self) -> None:
        if self._ligand_scope_is_sharded():
            self._prepare_ligand_shards()
            return
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
                    executor_name=self.ligand_prep_destination.executor_name(),
                )
        except Exception as exc:
            self._follow_prep(self.ligand_prep_jobs, job_ids)
            self._error("Prepare Ligands", exc)
            return
        self._follow_prep(self.ligand_prep_jobs, job_ids)
        self.stepper.set_current_index(2)

    def _follow_prep(self, follower, job_ids: dict[str, str], unit: str = "batches") -> None:
        # ponytail: one entry per program; they run side by side, so "step i of n" only
        # means "job i". Fine while every selected program shares one prep family (K=1).
        if job_ids:
            # The noun says ligand/receptor; the program is noise (families share one prep).
            follower.follow([(follower.noun, job_id) for job_id in job_ids.values()], unit=unit)

    def _set_sharded_prep_mode(self, sharded: bool) -> None:
        """A physical shard is already the HTP task, so no second batch control applies."""
        self.prepare_ligand_batch_label.setVisible(not sharded)
        self.prepare_ligand_batch_size.setVisible(not sharded)

    def _prepare_ligand_shards(self) -> None:
        """Same button, same programs, shard-sized work: no scope and no batch — one shard is one task.

        A sharded library has no rows to select — the "Force re-prepare" box still means what
        it says.
        """
        target = self._selected_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        job_ids: dict[str, str] = {}
        try:
            for program in programs:
                job_ids[program] = self.runtime.docking.prepare_ligand_shards(
                    program=program,
                    force=self.force_prepare_ligands.isChecked(),
                    executor_name=self.ligand_prep_destination.executor_name(),
                )
        except Exception as exc:
            self._follow_prep(self.ligand_prep_jobs, job_ids, unit="shards")
            self._error("Prepare Ligands", exc)
            return
        self._follow_prep(self.ligand_prep_jobs, job_ids, unit="shards")
        self.stepper.set_current_index(2)

    def _prepare_receptors(self) -> None:
        job_ids: dict[str, str] = {}
        target = self._selected_receptor_prep_target()
        programs = self._distinct_prep_programs(role="receptor") if target is None else [target]
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
                    executor_name=self.receptor_prep_destination.executor_name(),
                )
        except ValueError as exc:
            self._follow_prep(self.receptor_prep_jobs, job_ids)
            self._warn("Prepare Receptor", str(exc))
            return
        except Exception as exc:
            self._follow_prep(self.receptor_prep_jobs, job_ids)
            self._error("Prepare Receptor", exc)
            return
        self._follow_prep(self.receptor_prep_jobs, job_ids)
        self.stepper.set_current_index(3)

    def _save_prepare_ligands_to_workflow(self) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        # Capture current config now (widget-free submit). check_required=False: in a workflow this
        # waits for the 3D step, so the submit-time has_3d gate would always fail.
        scope = self._prep_ligand_scope()
        target = self._selected_prep_target()
        programs = self._distinct_prep_programs() if target is None else [target]
        batch = max(1, int(self.prepare_ligand_batch_size.value()))
        force = self.force_prepare_ligands.isChecked()
        # Captured now, not at run time: a workflow step keeps the destination it was saved with.
        executor = self.ligand_prep_destination.executor_name()

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
        programs = self._distinct_prep_programs(role="receptor") if target is None else [target]
        batch = max(1, int(self.prepare_receptor_batch_size.value()))
        force = self.force_prepare_receptors.isChecked()
        waters = self.keep_waters_receptors.isChecked()
        cofactors = self.keep_cofactors_receptors.isChecked()
        executor = self.receptor_prep_destination.executor_name()

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
