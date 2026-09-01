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




class ProtocolEditorWidget:
    """Program and protocol editor component for Docking Studio."""

    def _build_programs_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_experiment_setup(page))

        self.protocol_mode_box = QGroupBox("Software Run Set", page)
        protocol_layout = QVBoxLayout(self.protocol_mode_box)
        self.protocol_mode_label = QLabel("", self.protocol_mode_box)
        self.protocol_mode_label.setWordWrap(True)
        protocol_layout.addWidget(self.protocol_mode_label)

        # One horizontal sub-tab per program, each holding that software's run settings.
        self._program_checks: dict[str, QCheckBox] = {}
        self.program_subtabs = QTabWidget(page)
        for spec in list_docking_programs():
            self.program_subtabs.addTab(self._build_program_config(spec), spec.label)
        self._refresh_program_availability()
        protocol_layout.addWidget(self.program_subtabs)
        layout.addWidget(self.protocol_mode_box)
        layout.addWidget(self._build_protocols_box(page), 1)
        self._ensure_default_protocol()
        self._sync_protocol_ui()
        return page

    def _build_protocols_box(self, parent: QWidget) -> QWidget:
        box = QGroupBox("Validation Protocol Set", parent)
        self.protocol_set_box = box
        layout = QVBoxLayout(box)
        self.protocol_set_label = QLabel(
            "Redocking compares a bounded set of protocol variants. Add only complete variants "
            f"you intend to validate; maximum {MAX_REDOCKING_PROTOCOLS}. Rescoring is 'None' until a backend is integrated.",
            box,
        )
        self.protocol_set_label.setWordWrap(True)
        layout.addWidget(self.protocol_set_label)
        self.protocol_table = QTableWidget(0, 5, box)
        self.protocol_table.setHorizontalHeaderLabels(["Label", "Program", "Scoring", "Config", "Rescoring"])
        self.protocol_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.protocol_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.protocol_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.protocol_table.setMinimumHeight(180)
        self.protocol_table.currentCellChanged.connect(lambda *_args: self._load_selected_protocol_into_editor())
        layout.addWidget(self.protocol_table, 1)
        buttons = QHBoxLayout()
        self.add_protocol_btn = QPushButton("Add variant", box)
        self.add_protocol_btn.setToolTip("Add the current program settings as a redocking validation variant.")
        self.add_protocol_btn.clicked.connect(self._add_current_protocol)
        self.replace_protocol_btn = QPushButton("Update selected", box)
        self.replace_protocol_btn.clicked.connect(self._replace_selected_protocol)
        self.duplicate_protocol_btn = QPushButton("Duplicate", box)
        self.duplicate_protocol_btn.clicked.connect(self._duplicate_selected_protocol)
        self.remove_protocol_btn = QPushButton("Remove", box)
        self.remove_protocol_btn.clicked.connect(self._remove_selected_protocol)
        for button in (self.add_protocol_btn, self.replace_protocol_btn, self.duplicate_protocol_btn, self.remove_protocol_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return box

    def _build_experiment_setup(self, parent: QWidget) -> QWidget:
        box = QGroupBox("Experiment Setup", parent)
        form = QFormLayout(box)
        self.experiment_kind_combo = QComboBox(box)
        self.experiment_kind_combo.addItem("Docking", "docking")
        self.experiment_kind_combo.addItem("Redocking", "redocking")
        self.experiment_kind_combo.currentIndexChanged.connect(self._on_experiment_config_changed)
        self.receptor_type_combo = QComboBox(box)
        self.receptor_type_combo.addItem("Protein", MoleculeType.PROTEIN)
        self.receptor_type_combo.setToolTip("Only protein receptors are enabled for now; other receptor types will be added later.")
        self.ligand_type_combo = QComboBox(box)
        self.ligand_type_combo.addItem("Small molecule", MoleculeType.SMALL_MOLECULE)
        self.ligand_type_combo.setToolTip("Only small-molecule ligands are enabled for now; peptides/proteins will be added later.")
        for combo in (self.receptor_type_combo, self.ligand_type_combo):
            combo.currentIndexChanged.connect(self._on_experiment_config_changed)
        form.addRow("Experiment", self.experiment_kind_combo)
        form.addRow("Receptor type", self.receptor_type_combo)
        form.addRow("Ligand type", self.ligand_type_combo)
        return box

    def _build_program_config(self, spec) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        use = QCheckBox(f"Run {spec.label}", page)
        use.setToolTip("Docking mode: include this software as an independent job.")
        if spec.key == DEFAULT_PROGRAM:
            use.setChecked(True)
        use.toggled.connect(self.refresh)
        self._program_checks[spec.key] = use
        title = QLabel(
            f"{spec.label} settings. Docking runs selected software as independent jobs. "
            "Redocking snapshots these settings as validation variants.",
            page,
        )
        title.setWordWrap(True)
        form.addRow(use)
        form.addRow(title)
        if spec.key == VINA_PROGRAM.key:
            # Vina/AutoDock-Vina settings (shared by the Vina-family runners). CPU-per-task
            # lives here, not in the Run step: it's program-specific (Vina exposes it,
            # AutoDock4 does not), so it belongs to each program's own config.
            dd = self._docking_defaults()
            self.exhaustiveness = _spinbox(minimum=1, maximum=256, value=dd.exhaustiveness)
            self.num_modes = _spinbox(minimum=1, maximum=128, value=dd.num_modes)
            self.vina_cpu = _spinbox(minimum=1, maximum=128, value=dd.cpu_per_task)
            self.scoring_combo = QComboBox(page)
            self.scoring_combo.addItems(list(VINA_PROGRAM.scoring_functions))
            self.scoring_combo.setCurrentText("vina")
            self.backend_combo = QComboBox(page)
            self.backend_combo.addItem("binary")
            self.backend_combo.setCurrentText("binary")
            form.addRow("Exhaustiveness", self.exhaustiveness)
            form.addRow("Num modes", self.num_modes)
            form.addRow("CPU per task", self.vina_cpu)
            form.addRow("Scoring", self.scoring_combo)
            form.addRow("Backend", self.backend_combo)
        elif spec.key == GNINA_PROGRAM.key:
            # gnina reuses the Vina PDBQT prep; "Scoring" here is the --cnn_scoring mode.
            dd = self._docking_defaults()
            self.gnina_exhaustiveness = _spinbox(minimum=1, maximum=256, value=dd.exhaustiveness)
            self.gnina_num_modes = _spinbox(minimum=1, maximum=128, value=dd.num_modes)
            self.gnina_cpu = _spinbox(minimum=1, maximum=128, value=dd.cpu_per_task)
            self.gnina_cnn_combo = QComboBox(page)
            self.gnina_cnn_combo.addItems(list(GNINA_PROGRAM.scoring_functions))
            self.gnina_cnn_combo.setCurrentText("rescore")
            gpu_hint = QLabel(
                "CNN mode: rescore/none are CPU-bound; refinement/all are GPU-bound and run "
                "in the GPU token pool (≈1–2 at a time on a single card).",
                page,
            )
            gpu_hint.setWordWrap(True)
            form.addRow("Exhaustiveness", self.gnina_exhaustiveness)
            form.addRow("Num modes", self.gnina_num_modes)
            form.addRow("CPU per task", self.gnina_cpu)
            form.addRow("CNN scoring", self.gnina_cnn_combo)
            form.addRow(gpu_hint)
        else:
            hint = QLabel(
                "Uses engine defaults (AutoDockTools GPF/DPF). Per-program settings will be "
                "added here as engines are integrated.",
                page,
            )
            hint.setWordWrap(True)
            form.addRow(hint)
        return page

    def _docking_defaults(self):
        """User-configured docking defaults (amdockvs config, project layer included)."""
        from amdockvs.configuration import app_config

        return app_config(self.runtime).docking

    def _current_program_key(self) -> str:
        subtabs = getattr(self, "program_subtabs", None)
        specs = list(list_docking_programs())
        index = subtabs.currentIndex() if subtabs is not None else 0
        if 0 <= index < len(specs):
            return str(specs[index].key)
        return DEFAULT_PROGRAM

    @staticmethod
    def _program_label(program: str) -> str:
        for spec in list_docking_programs():
            if spec.key == program:
                return str(spec.label)
        return str(program)

    def _protocol_config_from_widgets(self, program: str) -> dict:
        if program == VINA_PROGRAM.key:
            return {
                "scoring_function": self.scoring_combo.currentText(),
                "exhaustiveness": int(self.exhaustiveness.value()),
                "num_modes": int(self.num_modes.value()),
                "vina_backend": self.backend_combo.currentText(),
                "vina_cpu": int(self.vina_cpu.value()),
            }
        if program == GNINA_PROGRAM.key:
            # gnina carries the --cnn_scoring mode in scoring_function (its own engine slot).
            return {
                "scoring_function": self.gnina_cnn_combo.currentText(),
                "exhaustiveness": int(self.gnina_exhaustiveness.value()),
                "num_modes": int(self.gnina_num_modes.value()),
                "vina_cpu": int(self.gnina_cpu.value()),
            }
        return {
            "num_modes": 9,
        }

    def _apply_protocol_config_to_widgets(self, protocol: dict) -> None:
        program = str(protocol.get("program") or DEFAULT_PROGRAM)
        config = dict(protocol.get("config") or {})
        specs = list(list_docking_programs())
        subtabs = getattr(self, "program_subtabs", None)
        if subtabs is not None:
            for index, spec in enumerate(specs):
                if spec.key == program:
                    subtabs.setCurrentIndex(index)
                    break
        check = (getattr(self, "_program_checks", {}) or {}).get(program)
        if check is not None:
            check.setChecked(True)
        dd = self._docking_defaults()
        if program == VINA_PROGRAM.key:
            self.scoring_combo.setCurrentText(str(config.get("scoring_function") or "vina"))
            self.exhaustiveness.setValue(int(config.get("exhaustiveness") or dd.exhaustiveness))
            self.num_modes.setValue(int(config.get("num_modes") or dd.num_modes))
            self.backend_combo.setCurrentText(str(config.get("vina_backend") or "binary"))
            self.vina_cpu.setValue(int(config.get("vina_cpu") or dd.cpu_per_task))
        elif program == GNINA_PROGRAM.key:
            self.gnina_cnn_combo.setCurrentText(str(config.get("scoring_function") or "rescore"))
            self.gnina_exhaustiveness.setValue(int(config.get("exhaustiveness") or dd.exhaustiveness))
            self.gnina_num_modes.setValue(int(config.get("num_modes") or dd.num_modes))
            self.gnina_cpu.setValue(int(config.get("vina_cpu") or dd.cpu_per_task))

    @staticmethod
    def _protocol_hash(program: str, config: dict, rescoring: list[dict] | None = None) -> str:
        return protocol_hash(program=program, config=config, rescoring=rescoring)

    def _protocol_label(self, program: str, config: dict, rescoring: list[dict] | None = None) -> str:
        # Same subset as the hash (`protocol_identity`): the label names what makes this protocol
        # different, so num_modes/backend/cpu stay out of it -- they don't move a pose.
        identity = protocol_identity(config)
        parts = [self._program_label(program)]
        scoring = str(identity.get("scoring_function") or "").strip()
        if scoring:
            parts.append(f"sf={scoring}")
        if "exhaustiveness" in identity:
            parts.append(f"exh={int(identity.get('exhaustiveness') or 8)}")
        rescoring_text = "None" if not rescoring else "+".join(str(item.get("program") or item) for item in rescoring)
        if rescoring_text != "None":
            parts.append(f"rerank={rescoring_text}")
        return " | ".join(parts)

    def _make_protocol(self, *, program: str, config: dict, label: str | None = None, rescoring: list[dict] | None = None) -> dict:
        normalized_config = dict(config or {})
        normalized_rescoring = list(rescoring or [])
        protocol_hash = self._protocol_hash(program, normalized_config, normalized_rescoring)
        return {
            "id": uuid4().hex,
            "schema": PROTOCOL_SCHEMA,
            "program": str(program),
            "label": str(label or self._protocol_label(program, normalized_config, normalized_rescoring)),
            "config": normalized_config,
            "rescoring": normalized_rescoring,
            "hash": protocol_hash,
        }

    def _protocol_for_program(self, program: str) -> dict:
        return self._make_protocol(program=program, config=self._protocol_config_from_widgets(program))

    def _current_protocol(self) -> dict:
        return self._protocol_for_program(self._current_program_key())

    def _ensure_default_protocol(self) -> None:
        if not self._protocols:
            self._protocols.append(self._current_protocol())
        if len(self._protocols) > MAX_REDOCKING_PROTOCOLS:
            self._protocols = self._protocols[:MAX_REDOCKING_PROTOCOLS]
        self._refresh_protocol_table()

    def _sync_protocol_ui(self) -> None:
        docking = self._run_kind() == "docking"
        mode_box = getattr(self, "protocol_mode_box", None)
        mode_label = getattr(self, "protocol_mode_label", None)
        set_box = getattr(self, "protocol_set_box", None)
        if mode_box is not None:
            mode_box.setTitle("Software Run Set" if docking else "Protocol Variant Editor")
        if mode_label is not None:
            if docking:
                mode_label.setText(
                    "Docking can run multiple selected software packages. Each selected software is enqueued "
                    "as an independent job; required preparations are derived from the selected software."
                )
            else:
                mode_label.setText(
                    "Redocking uses this area as an editor. Configure a variant here, then add or update it "
                    "in the validation set below."
                )
        for check in (getattr(self, "_program_checks", {}) or {}).values():
            check.setVisible(docking)
        if set_box is not None:
            set_box.setVisible(not docking)
        if not docking:
            self._ensure_default_protocol()

    def _refresh_protocol_table(self) -> None:
        table = getattr(self, "protocol_table", None)
        if table is None:
            return
        current_id = None
        current_row = table.currentRow()
        if 0 <= current_row < len(self._protocols):
            current_id = self._protocols[current_row].get("id")
        table.blockSignals(True)
        table.setRowCount(len(self._protocols))
        for row, protocol in enumerate(self._protocols):
            config = dict(protocol.get("config") or {})
            config_text = ", ".join(
                f"{key}={value}"
                for key, value in config.items()
                if key not in {"scoring_function"}
            )
            rescoring = list(protocol.get("rescoring") or [])
            values = [
                str(protocol.get("label") or ""),
                self._program_label(str(protocol.get("program") or "")),
                str(config.get("scoring_function") or "-"),
                config_text or "-",
                "None" if not rescoring else ", ".join(str(item.get("program") or item) for item in rescoring),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, str(protocol.get("id") or ""))
                table.setItem(row, column, item)
        table.blockSignals(False)
        if self._protocols:
            next_row = 0
            if current_id:
                for row, protocol in enumerate(self._protocols):
                    if protocol.get("id") == current_id:
                        next_row = row
                        break
            table.setCurrentCell(next_row, 0)
        table.resizeColumnsToContents()
        if hasattr(self, "receptor_scope_combo"):
            self.refresh()

    def _selected_protocol_row(self) -> int:
        table = getattr(self, "protocol_table", None)
        if table is None:
            return -1
        row = table.currentRow()
        return row if 0 <= row < len(self._protocols) else -1

    def _load_selected_protocol_into_editor(self) -> None:
        row = self._selected_protocol_row()
        if row < 0:
            return
        self._apply_protocol_config_to_widgets(self._protocols[row])

    def _add_current_protocol(self) -> None:
        if self._run_kind() != "redocking":
            self._warn("Protocols", "Protocol variants are only used for Redocking validation.")
            return
        if len(self._protocols) >= MAX_REDOCKING_PROTOCOLS:
            self._warn("Protocols", f"Redocking supports at most {MAX_REDOCKING_PROTOCOLS} protocol variants.")
            return
        protocol = self._current_protocol()
        if any(str(existing.get("hash") or "") == str(protocol.get("hash") or "") for existing in self._protocols):
            self._warn("Protocols", "This protocol variant already exists in the validation set.")
            return
        self._protocols.append(protocol)
        self._refresh_protocol_table()

    def _replace_selected_protocol(self) -> None:
        if self._run_kind() != "redocking":
            self._warn("Protocols", "Protocol variants are only used for Redocking validation.")
            return
        row = self._selected_protocol_row()
        if row < 0:
            self._add_current_protocol()
            return
        protocol = self._current_protocol()
        duplicate = any(
            index != row and str(existing.get("hash") or "") == str(protocol.get("hash") or "")
            for index, existing in enumerate(self._protocols)
        )
        if duplicate:
            self._warn("Protocols", "Another validation variant already has this exact configuration.")
            return
        self._protocols[row] = protocol
        self._refresh_protocol_table()

    def _duplicate_selected_protocol(self) -> None:
        if self._run_kind() != "redocking":
            return
        if len(self._protocols) >= MAX_REDOCKING_PROTOCOLS:
            self._warn("Protocols", f"Redocking supports at most {MAX_REDOCKING_PROTOCOLS} protocol variants.")
            return
        row = self._selected_protocol_row()
        if row < 0:
            return
        protocol = dict(self._protocols[row])
        protocol["id"] = uuid4().hex
        protocol["label"] = f"{protocol.get('label') or 'Protocol'} copy"
        self._protocols.insert(row + 1, protocol)
        self._refresh_protocol_table()

    def _remove_selected_protocol(self) -> None:
        if self._run_kind() != "redocking":
            return
        row = self._selected_protocol_row()
        if row < 0:
            return
        del self._protocols[row]
        if not self._protocols:
            self._protocols.append(self._current_protocol())
        self._refresh_protocol_table()

    def _selected_programs(self) -> list[str]:
        available = {spec.key for spec in self._available_program_specs()}
        selected: list[str] = []
        for protocol in self._selected_protocols():
            program = str(protocol.get("program") or "")
            if program in available and program not in selected:
                selected.append(program)
        return selected

    def _selected_protocols(self) -> list[dict]:
        available = {spec.key for spec in self._available_program_specs()}
        if self._run_kind() == "docking":
            protocols: list[dict] = []
            checks = getattr(self, "_program_checks", {}) or {}
            for spec in list_docking_programs():
                check = checks.get(spec.key)
                if spec.key in available and check is not None and check.isChecked():
                    protocols.append(self._protocol_for_program(str(spec.key)))
            return protocols
        protocols: list[dict] = []
        for protocol in getattr(self, "_protocols", []) or []:
            program = str(protocol.get("program") or "")
            if program in available:
                protocols.append(dict(protocol))
        return protocols

    def _program(self) -> str:
        # Preview/prepare/check use the first selected protocol; the run fans out over all.
        selected = self._selected_programs()
        if selected:
            return selected[0]
        available = [spec.key for spec in self._available_program_specs()]
        if available:
            return available[0]
        raise ValueError("No docking program is available for the selected experiment configuration.")

    def _run_kind(self) -> str:
        combo = getattr(self, "experiment_kind_combo", None)
        return str(combo.currentData() or "docking") if combo is not None else "docking"

    def _receptor_type(self) -> str:
        combo = getattr(self, "receptor_type_combo", None)
        return str(combo.currentData() or MoleculeType.PROTEIN) if combo is not None else MoleculeType.PROTEIN

    def _ligand_type(self) -> str:
        combo = getattr(self, "ligand_type_combo", None)
        return str(combo.currentData() or MoleculeType.SMALL_MOLECULE) if combo is not None else MoleculeType.SMALL_MOLECULE

    def _program_compatible(self, spec) -> bool:
        supports = getattr(spec, "supports", None)
        if not callable(supports):
            return True
        return bool(
            supports(
                receptor_type=self._receptor_type(),
                ligand_type=self._ligand_type(),
                experiment_kind=self._run_kind(),
            )
        )

    def _available_program_specs(self) -> list[object]:
        return [spec for spec in list_docking_programs() if self._program_compatible(spec)]

    def _refresh_program_availability(self) -> None:
        specs = list(list_docking_programs())
        available_keys = {spec.key for spec in specs if self._program_compatible(spec)}
        subtabs = getattr(self, "program_subtabs", None)
        for index, spec in enumerate(specs):
            available = spec.key in available_keys
            if subtabs is not None:
                if hasattr(subtabs, "setTabVisible"):
                    subtabs.setTabVisible(index, available)
                subtabs.setTabEnabled(index, available)
                tooltip = "" if available else "Not available for the selected experiment configuration."
                subtabs.setTabToolTip(index, tooltip)
            check = (getattr(self, "_program_checks", {}) or {}).get(spec.key)
            if check is not None:
                check.setEnabled(available)
                if not available:
                    check.setChecked(False)
        checked_available = [
            key for key, check in (getattr(self, "_program_checks", {}) or {}).items()
            if key in available_keys and check.isChecked()
        ]
        if not checked_available and available_keys:
            preferred = DEFAULT_PROGRAM if DEFAULT_PROGRAM in available_keys else sorted(available_keys)[0]
            check = self._program_checks.get(preferred)
            if check is not None:
                check.setChecked(True)
            if subtabs is not None:
                for index, spec in enumerate(specs):
                    if spec.key == preferred:
                        subtabs.setCurrentIndex(index)
                        break
        self._refresh_prep_targets()
        self._refresh_receptor_prep_targets()
        self._refresh_protocol_table()

    def _on_experiment_config_changed(self) -> None:
        self._refresh_program_availability()
        self._sync_protocol_ui()
        self._on_run_kind_changed()
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()
        self.refresh()

    def _on_run_kind_changed(self) -> None:
        redocking = self._run_kind() == "redocking"
        self._sync_ligand_scope_options()
        self._sync_ligand_table_filter()
        if hasattr(self, "run_ligand_scope_combo"):
            self.run_ligand_scope_combo.setEnabled(not redocking)
        if hasattr(self, "run_receptor_scope_combo"):
            self.run_receptor_scope_combo.setEnabled(not redocking)
        if hasattr(self, "run_button"):
            self.run_button.setText("Run Redocking" if redocking else "Run Docking")
        if hasattr(self, "check_status_label"):
            self._check_requirements()

    @staticmethod
    def _prep_engine_for(program: str) -> str:
        """The preparation family a program belongs to — what EngineState rows are keyed by.

        Every "is it prepared?" question is per family, so no caller may assume "ad4".
        """
        for spec in list_docking_programs():
            if spec.key == str(program):
                return str(spec.preparation_engine)
        return "ad4"

    def _distinct_prep_programs(self) -> list[str]:
        # Prepare once per distinct preparation_engine among selected programs (programs
        # that share an engine — e.g. AutoDock4 reuses Vina prep — collapse to one).
        specs = {spec.key: spec for spec in list_docking_programs()}
        chosen: dict[str, str] = {}
        for key in self._selected_programs():
            spec = specs.get(key)
            if spec is not None:
                chosen.setdefault(spec.preparation_engine, key)
        return list(chosen.values()) or [DEFAULT_PROGRAM]
