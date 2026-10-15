from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from amdockvs.chemistry.filtering import (
    SMALL_MOLECULE_FIELD_SPECS,
    SmallMoleculeFilterCriteria,
    SmallMoleculeFilterOperator,
    SmallMoleculeFilterRule,
)
from amdockvs.models.molecules import MoleculeType, MoleculeUsageClass
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.view_ids import FILTER_ID, LIGANDS_VIEW_ID, MOLECULES_VIEW_ID


# Exclusions written by this tool carry this reason prefix so Recover can tell "excluded by the
# filter" from exclusions owned by other tools (manual, diversity, ...) and never touch those.
FILTER_REASON_PREFIX = "ui.moltools.filter"


def _operator_label(operator: str) -> str:
    return {
        SmallMoleculeFilterOperator.LT: "<",
        SmallMoleculeFilterOperator.LTE: "<=",
        SmallMoleculeFilterOperator.GT: ">",
        SmallMoleculeFilterOperator.GTE: ">=",
        SmallMoleculeFilterOperator.EQ: "=",
        SmallMoleculeFilterOperator.HAS_ANY: "has any",
        SmallMoleculeFilterOperator.IS_EMPTY: "is empty",
    }.get(str(operator or ""), str(operator or ""))


class SmallMoleculeFilterRuleRow(QWidget):
    def __init__(self, *, on_remove, parent=None):
        super().__init__(parent)
        self._on_remove = on_remove
        layout = QHBoxLayout(self)
        # layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.field_combo = QComboBox(self)
        for field_name, spec in SMALL_MOLECULE_FIELD_SPECS.items():
            self.field_combo.addItem(str(spec.get("label") or field_name), field_name)
        self.operator_combo = QComboBox(self)
        self.int_value = QSpinBox(self)
        self.int_value.setRange(-1_000_000, 1_000_000)
        self.double_value = QDoubleSpinBox(self)
        self.double_value.setRange(-1_000_000.0, 1_000_000.0)
        self.double_value.setDecimals(4)
        self.double_value.setSingleStep(0.1)
        self.remove_button = QPushButton("Remove", self)

        self.field_combo.currentIndexChanged.connect(self._sync_from_field)
        self.remove_button.clicked.connect(self._on_remove)

        layout.addWidget(self.field_combo, 3)
        layout.addWidget(self.operator_combo, 2)
        layout.addWidget(self.int_value, 2)
        layout.addWidget(self.double_value, 2)
        layout.addWidget(self.remove_button, 1)

        self._sync_from_field()

    def _sync_from_field(self) -> None:
        field_name = str(self.field_combo.currentData() or "")
        spec = SMALL_MOLECULE_FIELD_SPECS.get(field_name, {})
        operators = tuple(spec.get("operators") or ())
        previous = str(self.operator_combo.currentData() or "")
        self.operator_combo.blockSignals(True)
        self.operator_combo.clear()
        for operator in operators:
            self.operator_combo.addItem(_operator_label(operator), operator)
        index = self.operator_combo.findData(previous)
        self.operator_combo.setCurrentIndex(index if index >= 0 else 0)
        self.operator_combo.blockSignals(False)

        kind = str(spec.get("kind") or "")
        self.int_value.setVisible(kind == "int")
        self.double_value.setVisible(kind == "float")

    def to_rule(self) -> SmallMoleculeFilterRule | None:
        field_name = str(self.field_combo.currentData() or "")
        operator = str(self.operator_combo.currentData() or "")
        spec = SMALL_MOLECULE_FIELD_SPECS.get(field_name)
        if spec is None or not operator:
            return None
        kind = str(spec.get("kind") or "")
        value: float | int | None = None
        if kind == "int":
            value = int(self.int_value.value())
        elif kind == "float":
            value = float(self.double_value.value())
        return SmallMoleculeFilterRule(field=field_name, operator=operator, value=value)


class SmallMoleculeFilterRulesWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._rows_layout = QVBoxLayout()
        # self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        controls = QHBoxLayout()
        self.add_rule_button = QPushButton("Add Rule", self)
        self.add_rule_button.clicked.connect(self.add_rule)
        controls.addWidget(self.add_rule_button)
        controls.addStretch(1)
        layout.addLayout(self._rows_layout)
        layout.addLayout(controls)

    def add_rule(self) -> None:
        row = SmallMoleculeFilterRuleRow(on_remove=lambda: self._remove_row(row), parent=self)
        self._rows_layout.addWidget(row)

    def _remove_row(self, row: SmallMoleculeFilterRuleRow) -> None:
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    def criteria(self) -> SmallMoleculeFilterCriteria:
        rules: list[SmallMoleculeFilterRule] = []
        for index in range(self._rows_layout.count()):
            item = self._rows_layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, SmallMoleculeFilterRuleRow):
                rule = widget.to_rule()
                if rule is not None:
                    rules.append(rule)
        return SmallMoleculeFilterCriteria(rules=tuple(rules))


class MoleculeTypePlaceholderWidget(QWidget):
    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(message, self)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)


class MoleculeFilterWidget(QWidget):
    # This tool's scope key on the Molecules table it borrows (BoundTableWidget.push_scope).
    _SCOPE_KEY = "filter"

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._ready = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        # outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to filter molecules.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        toolbar = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        self.open_ligands_button = QPushButton("Open Ligands", self)
        self.open_ligands_button.clicked.connect(self._open_ligands)
        toolbar.addWidget(self.open_ligands_button)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll, 1)

        body = QWidget(scroll)
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._body_layout = QVBoxLayout(body)
        # self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(8)
        scroll.setWidget(body)

        self.scope_box = QGroupBox("Source", self)
        scope_layout = QFormLayout(self.scope_box)
        self.molecule_type_combo = QComboBox(self.scope_box)
        self.molecule_type_combo.addItem("Small Molecules", MoleculeType.SMALL_MOLECULE)
        self.molecule_type_combo.addItem("Proteins", MoleculeType.PROTEIN)
        self.molecule_type_combo.addItem("Peptides", MoleculeType.PEPTIDE)
        self.molecule_type_combo.addItem("Nucleotides", MoleculeType.NUCLEOTIDE)
        self.molecule_type_combo.addItem("Polymers", MoleculeType.POLYMER)
        self.molecule_type_combo.currentIndexChanged.connect(self._sync_filter_stack)
        self.target_scope_combo = QComboBox(self.scope_box)
        self.target_scope_combo.addItem("Outside Sets", "outside_sets")
        self.target_scope_combo.addItem("Inside Sets", "inside_sets")
        self.target_scope_combo.addItem("All Molecules", "all_molecules")
        self.target_scope_combo.addItem("Specific Set", "specific_set")
        self.target_scope_combo.currentIndexChanged.connect(self._sync_scope_controls)
        self.set_combo = QComboBox(self.scope_box)
        self.usage_scope_combo = QComboBox(self.scope_box)
        self.usage_scope_combo.addItem("General", MoleculeUsageClass.GENERAL)
        self.usage_scope_combo.addItem("Reference", MoleculeUsageClass.REFERENCE)
        self.usage_scope_combo.addItem("Derived", MoleculeUsageClass.DERIVED)
        self.usage_scope_combo.addItem("General + Reference", (MoleculeUsageClass.GENERAL, MoleculeUsageClass.REFERENCE))
        self.usage_scope_combo.addItem("All", None)
        # The action implies the state scope + what Apply does — one direction only, so no run
        # ever flips a side it doesn't own (see FILTER_REASON_PREFIX).
        self.action_combo = QComboBox(self.scope_box)
        self.action_combo.addItem("Enrich — exclude failing molecules", "enrich")
        self.action_combo.addItem("Recover — re-include filter-excluded that now pass", "recover")
        self.action_combo.addItem("Tag matches to a set (no state change)", "tag")
        self.action_combo.currentIndexChanged.connect(self._sync_action_controls)
        self.set_name_edit = QLineEdit(self.scope_box)
        self.set_name_edit.setPlaceholderText("New set name")
        scope_layout.addRow("Molecule Type", self.molecule_type_combo)
        scope_layout.addRow("Action", self.action_combo)
        scope_layout.addRow("Target", self.target_scope_combo)
        scope_layout.addRow("Set", self.set_combo)
        scope_layout.addRow("Usage", self.usage_scope_combo)
        self.set_name_row_label = QLabel("Tag into set", self.scope_box)
        scope_layout.addRow(self.set_name_row_label, self.set_name_edit)
        self._body_layout.addWidget(self.scope_box)

        self.filter_box = QGroupBox("Filters", self)
        filter_layout = QVBoxLayout(self.filter_box)
        self.filter_stack = QStackedWidget(self.filter_box)
        self.small_molecule_rules_widget = SmallMoleculeFilterRulesWidget(self.filter_box)
        self.protein_placeholder = MoleculeTypePlaceholderWidget(
            "Protein filters are not defined yet for this workspace. Select Small Molecules to configure criteria.",
            self.filter_box,
        )
        self.peptide_placeholder = MoleculeTypePlaceholderWidget(
            "Peptide filters are not defined yet for this workspace. Select Small Molecules to configure criteria.",
            self.filter_box,
        )
        self.nucleotide_placeholder = MoleculeTypePlaceholderWidget(
            "Nucleotide filters are not defined yet for this workspace. Select Small Molecules to configure criteria.",
            self.filter_box,
        )
        self.polymer_placeholder = MoleculeTypePlaceholderWidget(
            "Polymer filters are not defined yet for this workspace. Select Small Molecules to configure criteria.",
            self.filter_box,
        )
        self.filter_stack.addWidget(self.small_molecule_rules_widget)
        self.filter_stack.addWidget(self.protein_placeholder)
        self.filter_stack.addWidget(self.peptide_placeholder)
        self.filter_stack.addWidget(self.nucleotide_placeholder)
        self.filter_stack.addWidget(self.polymer_placeholder)
        filter_layout.addWidget(self.filter_stack)
        self._body_layout.addWidget(self.filter_box)

        actions = QHBoxLayout()
        # One scan, not two: Apply runs the evaluation off-thread, shows the counts, and asks to
        # confirm before mutating. Cancelling is the old "Preview" — you see the result, nothing changes.
        self.apply_filter_button = QPushButton("Apply Filter", self)
        self.apply_filter_button.clicked.connect(self._apply_filter)
        actions.addWidget(self.apply_filter_button)
        actions.addStretch(1)
        self._body_layout.addLayout(actions)

        self.summary_label = QLabel("Ready.", self)
        self.summary_label.setWordWrap(True)
        self._body_layout.addWidget(self.summary_label)
        self._body_layout.addStretch(1)

        # Every control that changes what Apply would touch re-pushes the scope, so the table
        # shows the rows the run will scan — not a similar-looking selection of them.
        for combo in (
            self.molecule_type_combo,
            self.action_combo,
            self.target_scope_combo,
            self.set_combo,
            self.usage_scope_combo,
        ):
            combo.currentIndexChanged.connect(self._sync_molecules_scope)
        self._ready = True
        self.refresh()

    # --- the Molecules table this tool works on -------------------------------
    def _catalog_molecules_widget(self):
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(MOLECULES_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break the tool
            return None

    def _sync_molecules_scope(self) -> None:
        """Show the scope on Molecules, not on Ligands/Receptors: the filters are by molecular
        type, and entering by role drops every molecule whose type has no role."""
        widget = self._catalog_molecules_widget()
        if widget is None:
            return
        try:
            # The same public scope Apply will run — one source, so preview cannot drift.
            clause = self.runtime.molecules.scope_clause(self._capture_scope())
        except Exception:  # noqa: BLE001 - an incomplete scope (e.g. no set picked) shows nothing
            clause = None
        widget.push_scope(
            self._SCOPE_KEY,
            clause=clause,
            empty_message="Nothing in this scope to filter",
            show_action=False,
        )

    def showEvent(self, event):
        super().showEvent(event)
        if not self._ready:
            return
        opener = getattr(self.window(), "open_or_focus_view", None)
        if callable(opener):
            opener(MOLECULES_VIEW_ID)
        self._sync_molecules_scope()

    def hideEvent(self, event):
        super().hideEvent(event)
        widget = self._catalog_molecules_widget() if self._ready else None
        if widget is not None:
            widget.pop_scope(self._SCOPE_KEY)

    def _list_molecule_sets(self):
        return self.runtime.molecules.list_sets()

    def _selected_set_ref(self):
        data = self.set_combo.currentData()
        return self.runtime.molecules.resolve_set(int(data)) if data not in (None, "") else None

    def _selected_target_scope(self) -> str:
        return str(self.target_scope_combo.currentData() or "outside_sets").strip().lower() or "outside_sets"

    def _sync_scope_controls(self) -> None:
        self.set_combo.setEnabled(self._selected_target_scope() == "specific_set")

    def _selected_molecule_type(self) -> str:
        return str(self.molecule_type_combo.currentData() or MoleculeType.SMALL_MOLECULE)

    def _sync_filter_stack(self) -> None:
        molecule_type = self._selected_molecule_type()
        mapping = {
            MoleculeType.SMALL_MOLECULE: 0,
            MoleculeType.PROTEIN: 1,
            MoleculeType.PEPTIDE: 2,
            MoleculeType.NUCLEOTIDE: 3,
            MoleculeType.POLYMER: 4,
        }
        self.filter_stack.setCurrentIndex(mapping.get(molecule_type, 0))
        title_map = {
            MoleculeType.SMALL_MOLECULE: "Small Molecule Filters",
            MoleculeType.PROTEIN: "Protein Filters",
            MoleculeType.PEPTIDE: "Peptide Filters",
            MoleculeType.NUCLEOTIDE: "Nucleotide Filters",
            MoleculeType.POLYMER: "Polymer Filters",
        }
        self.filter_box.setTitle(title_map.get(molecule_type, "Filters"))

    def _populate_scope_combo(self) -> None:
        selected = self.set_combo.currentData()
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        self.set_combo.addItem("Select a set", None)
        for record in self._list_molecule_sets():
            label = f"#{int(record.id or 0)}  {record.name or 'unnamed_set'}"
            purpose = str(getattr(record, "purpose", "") or "").strip()
            if purpose:
                label = f"{label} [{purpose}]"
            self.set_combo.addItem(label, int(record.id or 0))
        index = self.set_combo.findData(selected)
        self.set_combo.setCurrentIndex(index if index >= 0 else 0)
        self.set_combo.blockSignals(False)

    def _selected_action(self) -> str:
        return str(self.action_combo.currentData() or "enrich").strip().lower() or "enrich"

    def _sync_action_controls(self) -> None:
        # The action owns the state scope, so the only extra control it drives is the set name,
        # relevant to Tag alone.
        is_tag = self._selected_action() == "tag"
        self.set_name_edit.setVisible(is_tag)
        self.set_name_row_label.setVisible(is_tag)
        self.apply_filter_button.setText("Tag to Set" if is_tag else "Apply Filter")

    def _action_excluded_value(self) -> bool:
        # Enrich/Tag scan active molecules; Recover scans the ones the filter excluded.
        return self._selected_action() == "recover"

    def _source_label(self) -> str:
        action = self._selected_action()
        target_scope = self._selected_target_scope()
        selected_set = self._selected_set_ref()
        usage_label = self.usage_scope_combo.currentText().strip().lower()
        where = {
            "specific_set": f"set #{selected_set.id if selected_set is not None else ''}",
            "inside_sets": "inside sets",
            "all_molecules": "all molecules",
        }.get(target_scope, "outside sets")
        return f"{where}, action={action}, usage={usage_label}"

    def _selected_usage_scope(self):
        return self.usage_scope_combo.currentData()

    def _capture_scope(self):
        # Reads widgets → must run on the GUI thread. Returns a scope the worker can stream from.
        target_scope = self._selected_target_scope()
        selected_set = self._selected_set_ref() if target_scope == "specific_set" else None
        if target_scope == "specific_set" and selected_set is None:
            raise ValueError("Select a set before applying a set-scoped filter.")
        in_set: bool | None = None
        if target_scope == "outside_sets":
            in_set = False
        elif target_scope == "inside_sets":
            in_set = True
        return self.runtime.molecules.select(
            source=selected_set,
            molecule_type=self._selected_molecule_type(),
            excluded=self._action_excluded_value(),
            in_set=in_set,
            usage_class=self._selected_usage_scope(),
        )

    def _filter_plan(self) -> dict[str, Any]:
        # GUI thread: capture widgets into domain values; workers never touch Qt objects.
        if self._selected_molecule_type() != MoleculeType.SMALL_MOLECULE:
            raise NotImplementedError("Filter criteria are only implemented for small molecules right now.")
        action = self._selected_action()
        set_name = self.set_name_edit.text().strip()
        if action == "tag" and not set_name:
            raise ValueError("Enter a set name to tag matches into.")
        return {
            "action": action,
            "criteria": self.small_molecule_rules_widget.criteria(),
            "scope": self._capture_scope(),
            # Recover only re-includes exclusions this filter owns — never manual/diversity ones.
            "exclusion_reason_prefix": FILTER_REASON_PREFIX if action == "recover" else "",
            "set_name": set_name,
        }

    def _update_summary(self, action: str, counts: dict[str, int]) -> None:
        matched, nonmatched, skipped = counts["matched"], counts["nonmatched"], counts["skipped"]
        if action == "recover":
            outcome = f"Will re-activate {matched} that now pass; {nonmatched} stay excluded."
        elif action == "tag":
            outcome = f"Will tag {matched} match(es) into a set."
        else:
            outcome = f"Will exclude {nonmatched} that fail; {matched} pass and stay active."
        text = (
            f"Scope: {self._source_label()}. "
            f"Scanned {counts['scanned']} molecule(s). Matched {matched}. {outcome}"
        )
        if skipped:
            # Skipped = no computed descriptors → not evaluable. Surface it, don't bury it as "no match".
            text += (
                f"\n⚠ {skipped} molecule(s) have no computed properties and were skipped "
                f"(compute descriptors at import to include them)."
            )
        if counts["evaluable"] == 0 and counts["scanned"]:
            text += "\n⚠ Nothing was evaluable in this scope — no valid result."
        self.summary_label.setText(text)

    def _open_ligands(self) -> None:
        main_window = self.window()
        if hasattr(main_window, "open_or_focus_view"):
            main_window.open_or_focus_view(LIGANDS_VIEW_ID)

    def _on_evaluation_error(self, exc: Exception) -> None:
        self.apply_filter_button.setEnabled(True)
        QMessageBox.critical(self, "Molecule Filter", str(exc))

    def _apply_filter(self) -> None:
        # Count with COUNT(*) off the GUI thread, show the numbers, confirm, then mutate with a single
        # UPDATE ... WHERE — nothing is materialized in Python, so it scales to millions of rows.
        try:
            plan = self._filter_plan()
        except Exception as exc:
            QMessageBox.critical(self, "Molecule Filter", str(exc))
            return
        self.apply_filter_button.setEnabled(False)
        run_async(
            lambda: self.runtime.molecules.evaluate_filter(
                plan["scope"],
                plan["criteria"],
                exclusion_reason_prefix=plan["exclusion_reason_prefix"],
            ),
            lambda counts: self._on_counts(plan, counts),
            on_error=self._on_evaluation_error,
            busy=self.summary_label,
            compact=True,
        )

    def _on_counts(self, plan: dict[str, Any], counts: dict[str, int]) -> None:
        self.apply_filter_button.setEnabled(True)
        self._update_summary(plan["action"], counts)
        if counts["evaluable"] == 0:
            QMessageBox.information(self, "Apply Molecule Filter", "No evaluable molecules were found in the selected scope.")
            return
        confirm = QMessageBox.question(
            self,
            "Apply Molecule Filter",
            f"{self.summary_label.text()}\n\nApply this?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:  # Cancel = preview: counts shown, nothing mutated
            return
        action = plan["action"]
        self.apply_filter_button.setEnabled(False)
        work = lambda: self.runtime.molecules.apply_filter(
            plan["scope"],
            plan["criteria"],
            action=action,
            reason=f"{FILTER_REASON_PREFIX}:{action}",
            exclusion_reason_prefix=plan["exclusion_reason_prefix"],
            set_name=plan["set_name"],
        )
        run_async(
            work,
            lambda result: self._on_applied(plan, counts, result),
            on_error=self._on_evaluation_error,
            busy=self.summary_label,
            compact=True,
        )

    def _on_applied(self, plan: dict[str, Any], counts: dict[str, int], result) -> None:
        self.apply_filter_button.setEnabled(True)
        action = plan["action"]
        if action == "tag":
            tagged, set_id = result
            message = f"Tagged {tagged} match(es) into set '{plan['set_name']}' (#{set_id})."
        else:
            activated, excluded = result
            message = (
                f"Re-activated {activated} molecule(s)." if action == "recover"
                else f"Excluded {excluded} molecule(s) that failed the filter."
            ) + f"\nSkipped {counts['skipped']} without computed properties."
        QMessageBox.information(self, "Apply Molecule Filter", message)
        self.refresh()

    def refresh(self) -> None:
        self.runtime.molecules.sync_set_membership()
        self._populate_scope_combo()
        self._sync_scope_controls()
        self._sync_action_controls()
        self._sync_filter_stack()
        self.summary_label.setText("Ready.")


def register_filter_workspace(window) -> None:
    window.register_main_view(
        FILTER_ID,
        "Molecule Filter",
        lambda: MoleculeFilterWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = [
    "MoleculeFilterWidget",
    "register_filter_workspace",
]
