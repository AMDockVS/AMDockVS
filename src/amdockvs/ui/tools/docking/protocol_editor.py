from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget, )

from amdockvs.docking.engines.programs import (
    GNINA_PROGRAM, QVINA_PROGRAM, VINA_PROGRAM, list_docking_programs, missing_programs, unavailable_programs)
from amdockvs.docking.engines.autogrid import AUTOGRID_UNAVAILABLE
from amdockvs.docking.methods import METHODS, get_method
from amdockvs.integrations.registry import MANAGED_TOOLS
from amdockvs.ui.common.async_query import run_async
from amdockvs.docking.protocols import PROTOCOL_SCHEMA, protocol_hash, protocol_identity
from amdockvs.core.vocab import MoleculeType
from ms_components.tool_panel import ToolPanel

# What a docking run starts ticked with: the first of these that can run here. QuickVina 2 scores
# with Vina's function and gets there sooner; Vina needs no install, so there is always one.
DEFAULT_PROGRAMS = (QVINA_PROGRAM.key, VINA_PROGRAM.key)
MAX_REDOCKING_PROTOCOLS = 12


def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


class ProtocolEditorWidget:
    """Program and protocol editor component for Docking Studio."""

    def _build_programs_tab(self) -> QWidget:
        panel = ToolPanel(self)
        self._build_experiment_setup(panel.add_section("Experiment Setup"))

        self.protocol_mode_box = panel.add_section("Software Run Set")
        self.protocol_mode_label = QLabel("", self.protocol_mode_box)
        self.protocol_mode_label.setWordWrap(True)
        self.protocol_mode_box.add_row(self.protocol_mode_label)

        # Programs stacked in a list on the left (it keeps reading as programs are added), the
        # selected one's run settings on the right. Rows follow list_docking_programs() order.
        self._program_checks: dict[str, QCheckBox] = {}
        self._program_config_widgets: dict[str, dict[str, QWidget]] = {}
        # A program that is not on this machine stays in the list, marked, with the way to it.
        self._missing_programs: dict[str, str] = {}
        self._program_notices: dict[str, tuple[QFormLayout, QWidget, QLabel]] = {}
        self.program_list = QListWidget(self.protocol_mode_box)
        self.program_pages = QStackedWidget(self.protocol_mode_box)
        for spec in list_docking_programs():
            self.program_list.addItem(spec.label)
            self.program_pages.addWidget(self._build_program_config(spec))
        self.program_list.setSpacing(2)
        self.program_list.setFixedWidth(self.program_list.sizeHintForColumn(0) + 32)
        self.program_list.currentRowChanged.connect(self.program_pages.setCurrentIndex)
        self.program_list.setCurrentRow(0)
        self._refresh_program_availability()
        programs = QHBoxLayout()
        programs.addWidget(self.program_list)
        programs.addWidget(self.program_pages, 1)
        self.protocol_mode_box.add_row(programs)
        self._build_protocols_box(panel.add_section("Validation Protocol Set"))
        self._ensure_default_protocol()  # redocking starts from a Vina variant: the one that runs anywhere
        self._select_program(next((key for key, check in self._program_checks.items() if check.isChecked()), ""))
        self._sync_protocol_ui()
        self.program_list.currentRowChanged.connect(lambda _row: self._sync_variant_buttons())
        self._check_programs()
        from amdockvs.ui.shell.settings_tools import tool_events
        tool_events.installed.connect(self._check_programs)
        next_button = QPushButton("Next", panel)
        next_button.setToolTip("Continue to Ligands to select and prepare ligands.")
        next_button.clicked.connect(self.stepper.next)
        panel.action_bar.add_action(next_button)
        return panel

    def _build_protocols_box(self, box) -> None:
        self.protocol_set_box = box
        self.protocol_set_label = QLabel(
            "Redocking compares a bounded set of protocol variants. Add only complete variants "
            f"you intend to validate; maximum {MAX_REDOCKING_PROTOCOLS}. Rescoring is 'None' until a backend is integrated.",
            box,
        )
        self.protocol_set_label.setWordWrap(True)
        box.add_row(self.protocol_set_label)
        self.protocol_table = QTableWidget(0, 5, box)
        self.protocol_table.setHorizontalHeaderLabels(["Label", "Program", "Scoring", "Config", "Rescoring"])
        self.protocol_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.protocol_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.protocol_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.protocol_table.setMinimumHeight(180)
        self.protocol_table.currentCellChanged.connect(lambda *_args: self._load_selected_protocol_into_editor())
        box.add_row(self.protocol_table)
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
        for button in (self.add_protocol_btn, self.replace_protocol_btn, self.duplicate_protocol_btn,
                       self.remove_protocol_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        box.add_row(buttons)

    def _build_experiment_setup(self, box) -> None:
        form = box.form
        self.experiment_kind_combo = QComboBox(box)
        self.experiment_kind_combo.addItem("Docking", "docking")
        self.experiment_kind_combo.addItem("Redocking", "redocking")
        self.experiment_kind_combo.currentIndexChanged.connect(self._on_experiment_config_changed)
        # The method decides which programs/scoring run and which receptors fit (methods.py).
        self.method_combo = QComboBox(box)
        for method in METHODS.values():
            self.method_combo.addItem(method.label, method.key)
        self.method_combo.setToolTip(
            "Zinc metalloprotein: AutoDock4Zn maps, run by Vina (ad4 scoring) or AutoDock-GPU, and only "
            "receptors with a zinc atom in the box. Standard leaves those receptors out.")
        if AUTOGRID_UNAVAILABLE:  # AutoDock4Zn is autogrid4 maps, nothing else
            zinc = self.method_combo.model().item(self.method_combo.findData("zinc"))
            zinc.setEnabled(False)
            zinc.setToolTip(AUTOGRID_UNAVAILABLE)
        self.receptor_type_combo = QComboBox(box)
        self.receptor_type_combo.addItem("Protein", MoleculeType.PROTEIN)
        self.receptor_type_combo.setToolTip(
            "Only protein receptors are enabled for now; other receptor types will be added later.")
        self.ligand_type_combo = QComboBox(box)
        self.ligand_type_combo.addItem("Small molecule", MoleculeType.SMALL_MOLECULE)
        self.ligand_type_combo.setToolTip(
            "Only small-molecule ligands are enabled for now; peptides/proteins will be added later.")
        for combo in (self.method_combo, self.receptor_type_combo, self.ligand_type_combo):
            combo.currentIndexChanged.connect(self._on_experiment_config_changed)
        form.addRow("Experiment", self.experiment_kind_combo)
        form.addRow("Method", self.method_combo)
        form.addRow("Receptor type", self.receptor_type_combo)
        form.addRow("Ligand type", self.ligand_type_combo)
        self._experiment_form = form

    def _build_program_config(self, spec) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        # Same form look as the ToolPanel sections around it.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        use = QCheckBox(f"Run {spec.label}", page)
        use.setToolTip("Docking mode: include this software as an independent job.")
        if spec.key == DEFAULT_PROGRAMS[0]:
            use.setChecked(True)
        use.toggled.connect(self.refresh)
        self._program_checks[spec.key] = use
        title = QLabel(
            f"{spec.label} settings. Docking runs selected software as independent jobs. "
            "Redocking snapshots these settings as validation variants.",
            page,
        )
        title.setWordWrap(True)
        notice = QWidget(page)
        notice_rows = QVBoxLayout(notice)  # the button under the text: beside it, a narrow dock clips it
        notice_rows.setContentsMargins(0, 0, 0, 0)
        notice_label = QLabel(notice)
        notice_label.setWordWrap(True)
        notice_rows.addWidget(notice_label)
        settings_button = QPushButton("Open Settings", notice)
        settings_button.clicked.connect(lambda _checked=False, key=spec.key: self._open_program_settings(key))
        notice_rows.addWidget(settings_button, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow(notice)
        form.setRowVisible(notice, False)
        self._program_notices[spec.key] = (form, notice, notice_label)
        form.addRow(use)
        form.addRow(title)
        if spec.key == VINA_PROGRAM.key:
            # Vina/AutoDock-Vina settings (shared by the Vina-family runners). CPU-per-task
            # lives here, not in the Run step: it's program-specific (Vina exposes it,
            # AutoDock-GPU does not), so it belongs to each program's own config.
            dd = self._docking_defaults().vina
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
            dd = self._docking_defaults().gnina
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
            self._build_schema_config(form, spec, page)
        return page

    def _build_schema_config(self, form: QFormLayout, spec, parent: QWidget) -> None:
        widgets: dict[str, QWidget] = {}
        defaults = spec.validate_config({})
        for name, field in spec.config_model.model_json_schema().get("properties", {}).items():
            if name == "method":  # Experiment Setup owns it
                continue
            field_type = field.get("type")
            choices = list(field.get("enum") or [])
            default = defaults.get(name)
            if choices:
                widget = QComboBox(parent)
                for choice in choices:
                    widget.addItem(str(choice), choice)
                widget.setCurrentIndex(max(0, widget.findData(default)))
            elif field_type == "integer":
                widget = QSpinBox(parent)
                widget.setRange(int(field.get("minimum", -1_000_000)), int(field.get("maximum", 1_000_000)))
                widget.setValue(int(default or 0))
            elif field_type == "number":
                widget = QDoubleSpinBox(parent)
                widget.setDecimals(6)
                widget.setRange(float(field.get("exclusiveMinimum", field.get("minimum", -1e9))),
                                float(field.get("maximum", 1e9)))
                widget.setValue(float(default or 0.0))
            elif field_type == "boolean":
                widget = QCheckBox(parent)
                widget.setChecked(bool(default))
            else:
                widget = QLineEdit(str(default or ""), parent)
                if field.get("format") == "file-path":
                    browse = widget.addAction(
                        self.style().standardIcon(QStyle.SP_DialogOpenButton), QLineEdit.TrailingPosition
                    )
                    browse.triggered.connect(lambda _checked=False, target=widget: self._browse_into(target))
            widget.setToolTip(str(field.get("description") or ""))
            widgets[name] = widget
            form.addRow(str(field.get("title") or name.replace("_", " ").title()), widget)
        self._program_config_widgets[spec.key] = widgets

    def _browse_into(self, target: QLineEdit) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Select file", target.text())
        if path:
            target.setText(path)

    def _schema_config_from_widgets(self, program: str) -> dict:
        values: dict[str, object] = {}
        for name, widget in self._program_config_widgets.get(program, {}).items():
            if isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                values[name] = widget.value()
            elif isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QLineEdit):
                values[name] = widget.text()
        return values

    def _docking_defaults(self):
        """User-configured docking defaults (amdockvs config, project layer included)."""
        from amdockvs.core.configuration import app_config

        return app_config(self.runtime).docking

    def _current_program_key(self) -> str:
        programs = getattr(self, "program_list", None)
        specs = list(list_docking_programs())
        index = programs.currentRow() if programs is not None else 0
        if 0 <= index < len(specs):
            return str(specs[index].key)
        return VINA_PROGRAM.key

    def _select_program(self, program: str) -> None:
        """Show the settings of `program`, when it is in the list."""
        programs = getattr(self, "program_list", None)
        keys = [spec.key for spec in list_docking_programs()]
        if programs is not None and program in keys:
            programs.setCurrentRow(keys.index(program))

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
        return self._schema_config_from_widgets(program)

    def _apply_protocol_config_to_widgets(self, protocol: dict) -> None:
        program = str(protocol.get("program") or VINA_PROGRAM.key)
        config = dict(protocol.get("config") or {})
        method = self.method_combo.findData(str(config.get("method") or "standard"))
        if method >= 0:
            self.method_combo.setCurrentIndex(method)  # first: it decides the scoring choices
        self._select_program(program)  # its settings; what a docking run has ticked is not touched
        docking = self._docking_defaults()
        if program == VINA_PROGRAM.key:
            dd = docking.vina
            self.scoring_combo.setCurrentText(str(config.get("scoring_function") or "vina"))
            self.exhaustiveness.setValue(int(config.get("exhaustiveness") or dd.exhaustiveness))
            self.num_modes.setValue(int(config.get("num_modes") or dd.num_modes))
            self.backend_combo.setCurrentText(str(config.get("vina_backend") or "binary"))
            self.vina_cpu.setValue(int(config.get("vina_cpu") or dd.cpu_per_task))
        elif program == GNINA_PROGRAM.key:
            dd = docking.gnina
            self.gnina_cnn_combo.setCurrentText(str(config.get("scoring_function") or "rescore"))
            self.gnina_exhaustiveness.setValue(int(config.get("exhaustiveness") or dd.exhaustiveness))
            self.gnina_num_modes.setValue(int(config.get("num_modes") or dd.num_modes))
            self.gnina_cpu.setValue(int(config.get("vina_cpu") or dd.cpu_per_task))
        else:
            for name, widget in self._program_config_widgets.get(program, {}).items():
                value = config.get(name)
                if value is None:
                    continue
                if isinstance(widget, QComboBox):
                    widget.setCurrentIndex(max(0, widget.findData(value)))
                elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                    widget.setValue(value)
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))

    @staticmethod
    def _protocol_hash(program: str, config: dict, rescoring: list[dict] | None = None) -> str:
        return protocol_hash(program=program, config=config, rescoring=rescoring)

    def _protocol_label(self, program: str, config: dict, rescoring: list[dict] | None = None) -> str:
        # Same subset as the hash (`protocol_identity`): the label names what makes this protocol
        # different, so num_modes/backend/cpu stay out of it -- they don't move a pose.
        identity = protocol_identity(config, program=program)
        parts = [self._program_label(program)]
        scoring = str(identity.get("scoring_function") or "").strip()
        if scoring:
            parts.append(f"sf={scoring}")
        if "exhaustiveness" in identity:
            parts.append(f"exh={int(identity.get('exhaustiveness') or 8)}")
        for key, value in identity.items():
            if key not in {"scoring_function", "exhaustiveness"}:
                parts.append(f"{key}={value}")
        rescoring_text = "None" if not rescoring else "+".join(str(item.get("program") or item) for item in rescoring)
        if rescoring_text != "None":
            parts.append(f"rerank={rescoring_text}")
        return " | ".join(parts)

    def _make_protocol(self, *, program: str, config: dict, label: str | None = None,
                       rescoring: list[dict] | None = None) -> dict:
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
        config = self._protocol_config_from_widgets(program)
        spec = next((spec for spec in list_docking_programs() if spec.key == program), None)
        if spec is not None and "method" in spec.config_model.model_fields:
            config["method"] = self._method()
        return self._make_protocol(program=program, config=config)

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
        if hasattr(self, "receptor_bar"):
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
        method = self._method()
        for protocol in getattr(self, "_protocols", []) or []:
            program = str(protocol.get("program") or "")
            if program in available and str((protocol.get("config") or {}).get("method") or "standard") == method:
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

    def _method(self) -> str:
        combo = getattr(self, "method_combo", None)
        return str(combo.currentData() or "standard") if combo is not None else "standard"

    def _receptor_type(self) -> str:
        combo = getattr(self, "receptor_type_combo", None)
        return str(combo.currentData() or MoleculeType.PROTEIN) if combo is not None else MoleculeType.PROTEIN

    def _ligand_type(self) -> str:
        combo = getattr(self, "ligand_type_combo", None)
        return str(
            combo.currentData() or MoleculeType.SMALL_MOLECULE) if combo is not None else MoleculeType.SMALL_MOLECULE

    def _program_compatible(self, spec) -> bool:
        if not get_method(self._method()).allows(spec.key):
            return False
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

    def _check_programs(self) -> None:
        """Ask, off the GUI thread, which programs are not on this machine."""
        run_async(lambda: missing_programs(self.runtime), self._apply_missing_programs, on_error=lambda _exc: None)

    def _apply_missing_programs(self, missing: dict[str, str]) -> None:
        previous = getattr(self, "_missing_programs", None) or {}
        self._missing_programs = dict(missing)
        greyed = self.palette().brush(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        for index, spec in enumerate(list_docking_programs()):
            reason = self._missing_programs.get(spec.key, "")
            item = self.program_list.item(index)
            why = "not available" if spec.key in unavailable_programs() else "not installed"
            item.setText(f"{spec.label} — {why}" if reason else spec.label)
            item.setData(Qt.ItemDataRole.ForegroundRole, greyed if reason else None)
            form, notice, label = self._program_notices[spec.key]
            label.setText(reason)
            form.setRowVisible(notice, bool(reason))
        self.program_list.setFixedWidth(self.program_list.sizeHintForColumn(0) + 32)
        self._sync_variant_buttons()
        self._refresh_program_availability()
        # Just installed (missing on the last check, there now): the user wants to run it.
        for key in set(previous) - set(self._missing_programs):
            check = self._program_checks.get(key)
            if check is not None and check.isEnabled():
                check.setChecked(True)
                self._select_program(key)

    def _sync_variant_buttons(self) -> None:
        # Redocking: a variant of a program that cannot run is not one to add to the set.
        missing = self._current_program_key() in self._missing_programs
        self.add_protocol_btn.setEnabled(not missing)
        self.replace_protocol_btn.setEnabled(not missing)

    def _open_program_settings(self, program: str) -> None:
        opener = getattr(self.window(), "open_settings", None)
        if callable(opener):
            managed = any(tool.tool_id == program for tool in MANAGED_TOOLS)
            opener(node="page:External tools" if managed else "amdockvs/docking")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # Back from Settings, where the program may just have been installed.
        if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and getattr(self, "_missing_programs", None)):
            self._check_programs()

    def _sync_scoring_choices(self) -> None:
        """Vina's scoring functions the method allows; an excluded current one is replaced."""
        combo = getattr(self, "scoring_combo", None)
        if combo is None:
            return
        method = get_method(self._method())
        model = combo.model()
        allowed = [row for row in range(combo.count()) if method.allows(VINA_PROGRAM.key, combo.itemText(row))
                   and not (AUTOGRID_UNAVAILABLE and combo.itemText(row) == "ad4")]  # ad4 scores on autogrid4 maps
        for row in range(combo.count()):
            model.item(row).setEnabled(row in allowed)
        if allowed and combo.currentIndex() not in allowed:
            combo.setCurrentIndex(allowed[0])

    def _refresh_program_availability(self) -> None:
        self._sync_scoring_choices()
        specs = list(list_docking_programs())
        available_keys = {spec.key for spec in specs if self._program_compatible(spec)}
        runnable_keys = available_keys - set(getattr(self, "_missing_programs", ()))
        programs = getattr(self, "program_list", None)
        for index, spec in enumerate(specs):
            available = spec.key in available_keys
            if programs is not None:
                programs.item(index).setHidden(not available)
            check = (getattr(self, "_program_checks", {}) or {}).get(spec.key)
            if check is not None:
                check.setEnabled(spec.key in runnable_keys)
                if spec.key not in runnable_keys:
                    check.setChecked(False)
        if programs is not None and programs.currentItem() is not None and programs.currentItem().isHidden():
            visible = [index for index, spec in enumerate(specs) if spec.key in available_keys]
            programs.setCurrentRow(visible[0] if visible else -1)
        checked_available = [
            key for key, check in (getattr(self, "_program_checks", {}) or {}).items()
            if key in runnable_keys and check.isChecked()
        ]
        if not checked_available and runnable_keys:
            preferred = next((key for key in DEFAULT_PROGRAMS if key in runnable_keys), sorted(runnable_keys)[0])
            check = self._program_checks.get(preferred)
            if check is not None:
                check.setChecked(True)
            self._select_program(preferred)
        self._refresh_prep_targets()
        self._refresh_receptor_prep_targets()
        self._refresh_protocol_table()

    def _sync_ligand_source_choice(self) -> None:
        """Offer shards vs rows only where both can exist, and only the choices that can run.

        The Ligands step's "Ligands from" boxes, shown in `htpvs` alone: they pick the store(s)
        for the whole experiment. Rows need curated ligand rows to exist; redocking re-docks
        the reference cocrystals, which are rows, so it hides the choice.
        """
        try:
            sharded, ligand_rows = self.runtime.molecules.library_shape()
        except Exception:  # noqa: BLE001 - no project open yet
            sharded, ligand_rows = False, 0
        redocking = self._run_kind() == "redocking"
        self.ligand_source_section.setVisible(bool(sharded) and not redocking)
        self.ligand_sources.set_rows_available(bool(ligand_rows))

    def _on_experiment_config_changed(self) -> None:
        self._sync_ligand_source_choice()
        self._refresh_program_availability()
        self._sync_protocol_ui()
        self._on_run_kind_changed()
        self._sync_ligand_table_filter()
        self._sync_receptor_table_filter()
        self.refresh()

    def _on_run_kind_changed(self) -> None:
        redocking = self._run_kind() == "redocking"
        self._sync_ligand_table_filter()
        self._focus_ligand_view()
        if hasattr(self, "run_button"):
            self.run_button.setText("Run Redocking" if redocking else "Run Docking")
        if hasattr(self, "check_status_label"):
            self._check_requirements()

    @staticmethod
    def _prep_engine_for(program: str) -> str:
        """The preparation family a program belongs to — what preparation rows are keyed by.

        Every "is it prepared?" question is per family, so no caller may assume "ad4".
        """
        for spec in list_docking_programs():
            if spec.key == str(program):
                return str(spec.preparation_engine)
        return "ad4"

    def _distinct_prep_programs(self, *, role: str = "ligand") -> list[str]:
        # Prepare once per distinct preparation_engine among selected programs (programs
        # that share an engine — e.g. AutoDock-GPU reuses Vina prep — collapse to one).
        specs = {spec.key: spec for spec in list_docking_programs()}
        chosen: dict[str, str] = {}
        for key in self._selected_programs():
            spec = specs.get(key)
            if spec is not None and bool(getattr(spec, f"requires_{role}_preparation")):
                chosen.setdefault(spec.preparation_engine, key)
        return list(chosen.values())
