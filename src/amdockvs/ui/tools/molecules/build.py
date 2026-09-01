from __future__ import annotations

import importlib.util
import shutil

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.ui.async_query import run_async
from amdockvs.ui.catalog.molecules import MOLECULES_VIEW_ID
from amdockvs.vocab import MoleculeType
from ms_components.ms_table import FilterOperator, FilterSpec

BUILD_ID = "moltools.build"


def _spinbox(*, minimum: int, maximum: int, value: int) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


def _double_spinbox(
    *,
    minimum: float,
    maximum: float,
    value: float,
    step: float = 0.1,
    decimals: int = 3,
) -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setSingleStep(step)
    widget.setValue(value)
    return widget


def _checkbox(text: str, *, checked: bool = False) -> QCheckBox:
    widget = QCheckBox(text)
    widget.setChecked(bool(checked))
    return widget


def _tool_notice(status) -> str:
    """Status line for a managed runtime; installing happens in Settings."""
    if status.installed:
        return str(status.message)
    return f"{status.message} Install it from Settings > External tools."


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(str(name)) is not None


def _executable_available(name: str) -> bool:
    return shutil.which(str(name)) is not None


class MoleculeBuildWidget(QWidget):
    # This tool's scope key on the Molecules table it borrows (BoundTableWidget.push_scope).
    _SCOPE_KEY = "build"

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._ready = False
        self._bound_molecules_table = None
        self._selected_ids = {
            MoleculeType.SMALL_MOLECULE: [],
            MoleculeType.PROTEIN: [],
        }
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        self.tabs = QTabWidget(self)
        outer.addWidget(self.tabs, 1)

        self.small_molecules_tab, small_layout = self._tab_page()
        self.proteins_tab, protein_layout = self._tab_page()
        self.tabs.addTab(self.small_molecules_tab, "Small molecules")
        self.tabs.addTab(self.proteins_tab, "Proteins")

        self.batch_size_ligands = _spinbox(minimum=1, maximum=4096, value=128)
        self.batch_size_receptors = _spinbox(minimum=1, maximum=256, value=8)
        small_scope_row = QHBoxLayout()
        small_scope_row.addWidget(QLabel("Scope", self.small_molecules_tab))
        self.small_molecule_scope_combo = self._scope_combo(self.small_molecules_tab)
        small_scope_row.addWidget(self.small_molecule_scope_combo)
        small_scope_row.addStretch(1)
        small_layout.addLayout(small_scope_row)
        small_batch_row = QHBoxLayout()
        small_batch_row.addWidget(QLabel("Batch size", self.small_molecules_tab))
        small_batch_row.addWidget(self.batch_size_ligands)
        small_batch_row.addStretch(1)
        small_layout.addLayout(small_batch_row)

        self.ligand_protonation_box = QGroupBox("Protonation", self.small_molecules_tab)
        ligand_protonation_layout = QVBoxLayout(self.ligand_protonation_box)
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Method", self.ligand_protonation_box))
        self.ligand_protonation_method = QComboBox(self.ligand_protonation_box)
        self.ligand_protonation_method.addItem("Dimorphite-DL", "dimorphite")
        self.ligand_protonation_method.addItem("OpenBabel", "openbabel")
        self.ligand_protonation_method.addItem("pKasso", "pkasso")
        self.ligand_protonation_method.addItem("Polar Hs", "polar_hydrogens")
        method_row.addWidget(self.ligand_protonation_method, 1)
        ligand_protonation_layout.addLayout(method_row)
        self.ligand_protonation_pages: dict[str, QWidget] = {}
        self._protonation_tool_ready = {
            # dimorphite_dl is a declared dependency, but a stale env still ships without it
            "dimorphite": _module_available("dimorphite_dl"),
            "openbabel": False,
            "pkasso": False,
            "polar_hydrogens": True,
        }

        dimorphite_page, dimorphite_layout = self._protonation_page(
            "Rule-based, pH-aware single-state assignment. Ambiguous microstates are not retained."
        )
        self.dimorphite_ph = _double_spinbox(minimum=0.0, maximum=14.0, value=7.4, step=0.1, decimals=1)
        dimorphite_layout.addRow("pH", self.dimorphite_ph)
        self.ligand_protonation_pages["dimorphite"] = dimorphite_page
        ligand_protonation_layout.addWidget(dimorphite_page)

        openbabel_page, openbabel_layout = self._protonation_page(
            "Fast pH-aware protonation through the OpenBabel command-line tool."
        )
        self.openbabel_ph = _double_spinbox(minimum=0.0, maximum=14.0, value=7.4, step=0.1, decimals=1)
        self.openbabel_status = QLabel("Checking runtime…", openbabel_page)
        self.openbabel_status.setWordWrap(True)
        openbabel_layout.addRow("pH", self.openbabel_ph)
        openbabel_layout.addRow(self.openbabel_status)
        self.ligand_protonation_pages["openbabel"] = openbabel_page
        ligand_protonation_layout.addWidget(openbabel_page)

        pkasso_page, pkasso_layout = self._protonation_page(
            "Selects only the most probable pH-specific state; microstate derivatives are not stored."
        )
        self.pkasso_ph = _double_spinbox(minimum=0.0, maximum=14.0, value=7.4, step=0.1, decimals=1)
        self.pkasso_model = QComboBox(pkasso_page)
        self.pkasso_model.addItem("MolGpKa (fast)", "molgpka")
        self.pkasso_model.addItem("MolGpKa + Uni-pKa (precise)", "mixed")
        self.pkasso_threads = _spinbox(minimum=1, maximum=128, value=1)
        self.pkasso_gpu = _checkbox("Request one GPU", checked=False)
        self.pkasso_model.currentIndexChanged.connect(self._sync_pkasso_options)
        self.pkasso_status = QLabel("Checking runtime…", pkasso_page)
        self.pkasso_status.setWordWrap(True)
        pkasso_layout.addRow("pH", self.pkasso_ph)
        pkasso_layout.addRow("Model", self.pkasso_model)
        pkasso_layout.addRow("Threads", self.pkasso_threads)
        pkasso_layout.addRow(self.pkasso_gpu)
        pkasso_layout.addRow(self.pkasso_status)
        self.ligand_protonation_pages["pkasso"] = pkasso_page
        ligand_protonation_layout.addWidget(pkasso_page)

        polar_page, _polar_layout = self._protonation_page(
            "Keeps explicit hydrogens only on N, O, P and S atoms; this is not pH prediction."
        )
        self.ligand_protonation_pages["polar_hydrogens"] = polar_page
        ligand_protonation_layout.addWidget(polar_page)

        self.run_protonate_ligands_button = QPushButton("Protonate", self.ligand_protonation_box)
        self.run_protonate_ligands_button.clicked.connect(self._run_protonate_ligands)
        ligand_protonation_layout.addWidget(self._action_row(
            self.ligand_protonation_box,
            self._workflow_button(self.ligand_protonation_box, self._save_protonate_ligands),
            self.run_protonate_ligands_button,
        ))
        small_layout.addWidget(self.ligand_protonation_box)

        self.ligand_3d_box = QGroupBox("3D Generation", self.small_molecules_tab)
        ligand_3d_layout = QFormLayout(self.ligand_3d_box)
        self.ligand_add_hs = _checkbox("Add explicit hydrogens", checked=True)
        self.ligand_optimize_3d = _checkbox("Optimize generated conformer", checked=True)
        self.ligand_random_seed = _spinbox(minimum=0, maximum=2**31 - 1, value=0xF00D)
        self.run_generate_3d_button = QPushButton("Generate 3D", self.ligand_3d_box)
        self.run_generate_3d_button.clicked.connect(self._run_generate_3d_ligands)
        ligand_3d_layout.addRow(self.ligand_add_hs)
        ligand_3d_layout.addRow(self.ligand_optimize_3d)
        ligand_3d_layout.addRow("Random Seed", self.ligand_random_seed)
        ligand_3d_layout.addRow(self._action_row(
            self.ligand_3d_box,
            self._workflow_button(self.ligand_3d_box, self._save_generate_3d_ligands),
            self.run_generate_3d_button,
        ))
        small_layout.addWidget(self.ligand_3d_box)

        self.ligand_conformers_box = QGroupBox("Conformers", self.small_molecules_tab)
        ligand_conf_layout = QFormLayout(self.ligand_conformers_box)
        self.ligand_num_conformers = _spinbox(minimum=1, maximum=500, value=20)
        self.ligand_prune_rms = _double_spinbox(minimum=0.0, maximum=10.0, value=0.5, step=0.1)
        self.ligand_conformer_seed = _spinbox(minimum=0, maximum=2**31 - 1, value=0xF00D)
        self.ligand_conformer_add_hs = _checkbox("Add explicit hydrogens", checked=True)
        self.ligand_conformer_optimize = _checkbox("Optimize conformers", checked=True)
        self.run_conformers_button = QPushButton("Generate Conformers", self.ligand_conformers_box)
        self.run_conformers_button.clicked.connect(self._run_generate_ligand_conformers)
        ligand_conf_layout.addRow("Num Conformers", self.ligand_num_conformers)
        ligand_conf_layout.addRow("Prune RMS", self.ligand_prune_rms)
        ligand_conf_layout.addRow("Random Seed", self.ligand_conformer_seed)
        ligand_conf_layout.addRow(self.ligand_conformer_add_hs)
        ligand_conf_layout.addRow(self.ligand_conformer_optimize)
        ligand_conf_layout.addRow(self._action_row(
            self.ligand_conformers_box,
            self._workflow_button(self.ligand_conformers_box, self._save_generate_ligand_conformers),
            self.run_conformers_button,
        ))
        small_layout.addWidget(self.ligand_conformers_box)

        self.ligand_min_box = QGroupBox("Minimization", self.small_molecules_tab)
        ligand_min_layout = QFormLayout(self.ligand_min_box)
        self.ligand_forcefield = QComboBox(self.ligand_min_box)
        self.ligand_forcefield.addItem("MMFF", "mmff")
        self.ligand_forcefield.addItem("UFF", "uff")
        self.ligand_max_iters = _spinbox(minimum=1, maximum=10000, value=200)
        self.run_ligand_minimize_button = QPushButton("Minimize", self.ligand_min_box)
        self.run_ligand_minimize_button.clicked.connect(self._run_minimize_ligands)
        ligand_min_layout.addRow("Forcefield", self.ligand_forcefield)
        ligand_min_layout.addRow("Max Iterations", self.ligand_max_iters)
        ligand_min_layout.addRow(self._action_row(
            self.ligand_min_box,
            self._workflow_button(self.ligand_min_box, self._save_minimize_ligands),
            self.run_ligand_minimize_button,
        ))
        small_layout.addWidget(self.ligand_min_box)
        small_layout.addStretch(1)

        protein_scope_row = QHBoxLayout()
        protein_scope_row.addWidget(QLabel("Scope", self.proteins_tab))
        self.protein_scope_combo = self._scope_combo(self.proteins_tab)
        protein_scope_row.addWidget(self.protein_scope_combo)
        protein_scope_row.addStretch(1)
        protein_layout.addLayout(protein_scope_row)
        protein_batch_row = QHBoxLayout()
        protein_batch_row.addWidget(QLabel("Batch size", self.proteins_tab))
        protein_batch_row.addWidget(self.batch_size_receptors)
        protein_batch_row.addStretch(1)
        protein_layout.addLayout(protein_batch_row)

        self.receptor_protonation_box = QGroupBox("Protonation", self.proteins_tab)
        receptor_protonation_layout = QFormLayout(self.receptor_protonation_box)
        self.receptor_protonation_method = QComboBox(self.receptor_protonation_box)
        self.receptor_protonation_method.addItem("Reduce", "reduce")
        self.receptor_protonation_method.addItem("PDB2PQR", "pdb2pqr")
        self.receptor_protonation_ph = _double_spinbox(
            minimum=0.0, maximum=14.0, value=7.0, step=0.1, decimals=1
        )
        self.receptor_protonation_forcefield = QComboBox(self.receptor_protonation_box)
        for label in ("AMBER", "CHARMM", "PARSE"):
            self.receptor_protonation_forcefield.addItem(label, label)
        self.run_protonate_receptors_button = QPushButton("Protonate", self.receptor_protonation_box)
        self.run_protonate_receptors_button.clicked.connect(self._run_protonate_receptors)
        receptor_protonation_layout.addRow("Method", self.receptor_protonation_method)
        receptor_protonation_layout.addRow("pH", self.receptor_protonation_ph)
        receptor_protonation_layout.addRow("Forcefield", self.receptor_protonation_forcefield)
        receptor_protonation_layout.addRow(self._action_row(
            self.receptor_protonation_box,
            self._workflow_button(self.receptor_protonation_box, self._save_protonate_receptors),
            self.run_protonate_receptors_button,
        ))
        protein_layout.addWidget(self.receptor_protonation_box)

        self.receptor_fix_box = QGroupBox("Fix Structure", self.proteins_tab)
        receptor_fix_layout = QFormLayout(self.receptor_fix_box)
        self.fix_missing_residues = _checkbox("Add missing residues", checked=True)
        self.fix_missing_atoms = _checkbox("Add missing atoms", checked=True)
        self.fix_replace_nonstandard = _checkbox("Replace nonstandard residues", checked=True)
        self.fix_remove_heterogens = _checkbox("Remove heterogens", checked=False)
        self.fix_keep_water = _checkbox("Keep water when removing heterogens", checked=True)
        self.receptor_fix_structure = self._structure_combo(self.receptor_fix_box)
        self.run_fix_receptors_button = QPushButton("Fix", self.receptor_fix_box)
        self.run_fix_receptors_button.clicked.connect(self._run_fix_receptors)
        receptor_fix_layout.addRow("Structure", self.receptor_fix_structure)
        receptor_fix_layout.addRow(self.fix_missing_residues)
        receptor_fix_layout.addRow(self.fix_missing_atoms)
        receptor_fix_layout.addRow(self.fix_replace_nonstandard)
        receptor_fix_layout.addRow(self.fix_remove_heterogens)
        receptor_fix_layout.addRow(self.fix_keep_water)
        receptor_fix_layout.addRow(self._action_row(
            self.receptor_fix_box,
            self._workflow_button(self.receptor_fix_box, self._save_fix_receptors),
            self.run_fix_receptors_button,
        ))
        protein_layout.addWidget(self.receptor_fix_box)

        # ponytail: shell only — ESMFold still fails on genuinely new sequences, so the whole
        # group stays disabled until a predictor is worth wiring to a job.
        self.receptor_predict_box = QGroupBox("3D Generation", self.proteins_tab)
        receptor_predict_layout = QFormLayout(self.receptor_predict_box)
        self.receptor_predictor = QComboBox(self.receptor_predict_box)
        self.receptor_predictor.addItem("ESMFold", "esmfold")
        self.run_predict_receptors_button = QPushButton("Predict", self.receptor_predict_box)
        receptor_predict_layout.addRow("Predictor", self.receptor_predictor)
        receptor_predict_layout.addRow(self._action_row(
            self.receptor_predict_box,
            self.run_predict_receptors_button,
        ))
        self.receptor_predict_box.setEnabled(False)
        self.receptor_predict_box.setToolTip(
            "Structure prediction from sequence is not wired yet."
        )
        protein_layout.addWidget(self.receptor_predict_box)

        self.receptor_min_box = QGroupBox("Minimization", self.proteins_tab)
        receptor_min_layout = QFormLayout(self.receptor_min_box)
        self.receptor_forcefields = QComboBox(self.receptor_min_box)
        self.receptor_forcefields.addItem("amber14-all.xml", ("amber14-all.xml",))
        self.receptor_forcefields.addItem("amber14-all + amber14/tip3p", ("amber14-all.xml", "amber14/tip3p.xml"))
        self.receptor_max_iterations = _spinbox(minimum=1, maximum=100000, value=500)
        self.receptor_tolerance = _double_spinbox(minimum=0.001, maximum=10000.0, value=10.0, step=0.5)
        self.run_receptor_minimize_button = QPushButton("Minimize", self.receptor_min_box)
        self.run_receptor_minimize_button.clicked.connect(self._run_minimize_receptors)
        receptor_min_layout.addRow("Forcefields", self.receptor_forcefields)
        receptor_min_layout.addRow("Max Iterations", self.receptor_max_iterations)
        receptor_min_layout.addRow("Tolerance (kJ/mol/nm)", self.receptor_tolerance)
        receptor_min_layout.addRow(self._action_row(
            self.receptor_min_box,
            self._workflow_button(self.receptor_min_box, self._save_minimize_receptors),
            self.run_receptor_minimize_button,
        ))
        protein_layout.addWidget(self.receptor_min_box)
        protein_layout.addStretch(1)

        self.receptor_protonation_method.currentIndexChanged.connect(
            self._sync_receptor_protonation_options
        )
        self.ligand_protonation_method.currentIndexChanged.connect(
            self._sync_small_molecule_protonation_options
        )
        self.small_molecule_scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.protein_scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.tabs.currentChanged.connect(self._on_scope_changed)
        self._sync_receptor_protonation_options()
        self._sync_pkasso_options()
        self._sync_small_molecule_protonation_options()
        self._ready = True
        self.refresh()

    def _protonation_page(self, description: str) -> tuple[QWidget, QFormLayout]:
        page = QWidget(self.ligand_protonation_box)
        layout = QFormLayout(page)
        label = QLabel(description, page)
        label.setWordWrap(True)
        layout.addRow(label)
        return page, layout

    def _tab_page(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget(scroll)
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(body)
        layout.setSpacing(8)
        scroll.setWidget(body)
        return scroll, layout

    @staticmethod
    def _scope_combo(parent: QWidget) -> QComboBox:
        combo = QComboBox(parent)
        combo.addItem("All", "all")
        combo.addItem("Selected", "selected")
        combo.addItem("Filtered", "filtered")
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        return combo

    @staticmethod
    def _structure_combo(parent: QWidget) -> QComboBox:
        combo = QComboBox(parent)
        combo.addItem("Current", "current")
        combo.addItem("Original", "original")
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        combo.setToolTip(
            "Fix from the immutable imported structure, or from the latest successful result."
        )
        return combo

    @staticmethod
    def _action_row(parent: QWidget, *buttons: QPushButton) -> QWidget:
        row_widget = QWidget(parent)
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        for button in buttons:
            row.addWidget(button)
        return row_widget

    # --- scope: one molecular type, shown on the Molecules table -------------------
    def _selected_molecule_type(self) -> str:
        if self.tabs.currentWidget() is self.proteins_tab:
            return str(MoleculeType.PROTEIN)
        return str(MoleculeType.SMALL_MOLECULE)

    def _scope_mode(self, molecule_type: str) -> str:
        combo = (
            self.protein_scope_combo
            if molecule_type == MoleculeType.PROTEIN
            else self.small_molecule_scope_combo
        )
        return str(combo.currentData() or "all")

    def _scope(self, molecule_type: str | None = None):
        """What the ops run on. A type scope, so nothing is lost to a missing role flag."""
        resolved_type = str(molecule_type or self._selected_molecule_type())
        scope = self.runtime.molecules.select(molecule_type=resolved_type)
        mode = self._scope_mode(resolved_type)
        if mode == "selected":
            ids = self._selected_ids[resolved_type]
        elif mode == "filtered":
            ids = self._filtered_molecule_ids()
        else:
            return scope
        return self.runtime.molecules.filter(scope, filters={"id__in": ids or [0]})

    def _catalog_molecules_widget(self):
        central = getattr(self.window(), "central_widget", None)
        if central is None:
            return None
        try:
            return central.open_view(MOLECULES_VIEW_ID)
        except Exception:  # noqa: BLE001 - a missing/failed view must not break the tool
            return None

    def _sync_molecules_scope(self) -> None:
        if not self.isVisible():
            return  # A0: off screen it borrows nothing; showEvent pushes the scope on return
        widget = self._catalog_molecules_widget()
        if widget is None:
            return
        molecule_type = self._selected_molecule_type()
        self._bind_molecules_table(widget)
        # Keep the complete type visible: Selected/Filtered choose what an operation processes,
        # while the table remains the surface where that selection/filter can be changed.
        widget.push_scope(
            self._SCOPE_KEY,
            filters=[
                FilterSpec(
                    "molecule_type",
                    FilterOperator.EQ,
                    molecule_type,
                    label="build_molecule_type",
                )
            ],
            structure_source="current",
            empty_message="Nothing of this type to build on",
            show_action=False,
        )

    def _bind_molecules_table(self, widget) -> None:
        table = getattr(widget, "table", None)
        if table is None or table is self._bound_molecules_table:
            return
        table.selection_changed.connect(self._on_molecule_selection_changed)
        self._bound_molecules_table = table
        self._on_molecule_selection_changed(table.get_selected_objects())

    def _on_molecule_selection_changed(self, molecules: list[object]) -> None:
        selected: dict[str, list[int]] = {}
        for molecule in molecules or []:
            molecule_type = str(getattr(molecule, "molecule_type", "") or "")
            molecule_id = int(getattr(molecule, "id", 0) or 0)
            if molecule_type in self._selected_ids and molecule_id > 0:
                selected.setdefault(molecule_type, []).append(molecule_id)
        # Table refreshes clear Qt selection; retain the last explicit non-empty choice per tab.
        for molecule_type, ids in selected.items():
            self._selected_ids[molecule_type] = sorted(set(ids))

    def _filtered_molecule_ids(self) -> list[int]:
        table = self._bound_molecules_table
        if table is None:
            widget = self._catalog_molecules_widget()
            table = getattr(widget, "table", None) if widget is not None else None
        if table is None:
            return []
        return [int(value) for value in table.all_filtered_ids() if int(value) > 0]

    def _on_scope_changed(self, *_args) -> None:
        self._sync_molecules_scope()

    def _sync_receptor_protonation_options(self, *_args) -> None:
        uses_pdb2pqr = self.receptor_protonation_method.currentData() == "pdb2pqr"
        self.receptor_protonation_ph.setEnabled(uses_pdb2pqr)
        self.receptor_protonation_forcefield.setEnabled(uses_pdb2pqr)

    def _sync_pkasso_options(self, *_args) -> None:
        mixed = self.pkasso_model.currentData() == "mixed"
        self.pkasso_gpu.setEnabled(mixed)
        if not mixed:
            self.pkasso_gpu.setChecked(False)

    def _sync_small_molecule_protonation_options(self, *_args) -> None:
        method = self._active_protonation_method()
        for name, page in self.ligand_protonation_pages.items():
            page.setVisible(name == method)
        self.run_protonate_ligands_button.setText(
            "Add polar Hs" if method == "polar_hydrogens" else "Protonate"
        )
        available = bool(self._protonation_tool_ready.get(method, False))
        self.run_protonate_ligands_button.setEnabled(available)
        self.run_protonate_ligands_button.setToolTip(
            "" if available else f"{method} is not available in this environment."
        )

    def _active_protonation_method(self) -> str:
        return str(self.ligand_protonation_method.currentData() or "dimorphite")

    def _refresh_protonation_tools(self) -> None:
        status = getattr(self.runtime.chemistry, "small_molecule_protonation_tool_status", None)
        if not callable(status):
            self.openbabel_status.setText("Runtime status unavailable.")
            self.pkasso_status.setText("Runtime status unavailable.")
            self._protonation_tool_ready.update(openbabel=False, pkasso=False)
            self._sync_small_molecule_protonation_options()
            return
        run_async(
            lambda: (
                status("openbabel"),
                status("pkasso"),
            ),
            self._apply_protonation_tool_status,
            on_error=lambda exc: self._show_protonation_tool_error("Runtime check failed", exc),
            compact=True,
        )

    def _apply_protonation_tool_status(self, statuses) -> None:
        openbabel, pkasso = statuses
        # Installing is Settings' job; this panel only reports what is available.
        self.openbabel_status.setText(_tool_notice(openbabel))
        self.pkasso_status.setText(_tool_notice(pkasso))
        self._protonation_tool_ready.update(
            openbabel=bool(openbabel.installed),
            pkasso=bool(pkasso.installed),
        )
        self._sync_small_molecule_protonation_options()

    def _show_protonation_tool_error(self, title: str, exc: Exception) -> None:
        self.openbabel_status.setText("Runtime status unavailable.")
        self.pkasso_status.setText("Runtime status unavailable.")
        self._protonation_tool_ready.update(openbabel=False, pkasso=False)
        self._sync_small_molecule_protonation_options()
        QMessageBox.warning(self, title, str(exc))

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

    def _append_status(self, message: str) -> None:
        del message

    def _submit(self, title: str, callback) -> None:
        try:
            job_id = callback()
        except Exception as exc:
            QMessageBox.critical(self, title, str(exc))
            return
        QMessageBox.information(self, title, f"Submitted job {job_id}.")

    @staticmethod
    def _workflow_button(parent, handler) -> QPushButton:
        button = QPushButton("Save to workflow", parent)
        button.setToolTip("Add this op (current settings) as a workflow step — updates it if already there.")
        button.clicked.connect(handler)
        return button

    def _require_optional_module(self, *, title: str, modules: tuple[str, ...], detail: str) -> bool:
        missing = [name for name in modules if not _module_available(name)]
        if not missing:
            return True
        QMessageBox.warning(
            self,
            title,
            f"{detail}\nMissing module(s): {', '.join(missing)}",
        )
        self._append_status(f"{title}: unavailable, missing module(s): {', '.join(missing)}")
        return False

    def _require_executable(self, *, title: str, executable: str, detail: str) -> bool:
        if _executable_available(executable):
            return True
        QMessageBox.warning(
            self,
            title,
            f"{detail}\nMissing executable: {executable}",
        )
        self._append_status(f"{title}: unavailable, missing executable: {executable}")
        return False

    # Each op exposes its CURRENT config as a dict (kind == chemistry API method name), so Run and
    # Save-to-workflow share one source of truth and stay in sync.
    def _cfg_protonate_ligands(self) -> dict:
        method = self._active_protonation_method()
        ph = 7.4
        if method == "dimorphite":
            ph = float(self.dimorphite_ph.value())
        elif method == "openbabel":
            ph = float(self.openbabel_ph.value())
        elif method == "pkasso":
            ph = float(self.pkasso_ph.value())
        return dict(
            ligands=self._scope(MoleculeType.SMALL_MOLECULE),
            method=method,
            ph=ph,
            model=str(self.pkasso_model.currentData() or "molgpka"),
            threads=int(self.pkasso_threads.value()),
            gpu=self.pkasso_gpu.isChecked(),
            batch_size=int(self.batch_size_ligands.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_generate_3d_ligands(self) -> dict:
        return dict(
            ligands=self._scope(MoleculeType.SMALL_MOLECULE),
            add_hs=self.ligand_add_hs.isChecked(),
            random_seed=int(self.ligand_random_seed.value()),
            optimize=self.ligand_optimize_3d.isChecked(),
            batch_size=int(self.batch_size_ligands.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_generate_ligand_conformers(self) -> dict:
        return dict(
            ligands=self._scope(MoleculeType.SMALL_MOLECULE),
            num_conformers=int(self.ligand_num_conformers.value()),
            add_hs=self.ligand_conformer_add_hs.isChecked(),
            random_seed=int(self.ligand_conformer_seed.value()),
            prune_rms_thresh=float(self.ligand_prune_rms.value()),
            optimize=self.ligand_conformer_optimize.isChecked(),
            batch_size=int(self.batch_size_ligands.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_minimize_ligands(self) -> dict:
        return dict(
            ligands=self._scope(MoleculeType.SMALL_MOLECULE),
            forcefield=str(self.ligand_forcefield.currentData() or "mmff"),
            max_iters=int(self.ligand_max_iters.value()),
            batch_size=int(self.batch_size_ligands.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_protonate_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            method=str(self.receptor_protonation_method.currentData() or "reduce"),
            ph=float(self.receptor_protonation_ph.value()),
            forcefield=str(self.receptor_protonation_forcefield.currentData() or "AMBER"),
            batch_size=int(self.batch_size_receptors.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_fix_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            add_missing_residues=self.fix_missing_residues.isChecked(),
            add_missing_atoms=self.fix_missing_atoms.isChecked(),
            replace_nonstandard=self.fix_replace_nonstandard.isChecked(),
            remove_heterogens=self.fix_remove_heterogens.isChecked(),
            keep_water=self.fix_keep_water.isChecked(),
            structure_source=str(self.receptor_fix_structure.currentData() or "current"),
            batch_size=int(self.batch_size_receptors.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _cfg_minimize_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            forcefields=tuple(self.receptor_forcefields.currentData() or ("amber14-all.xml",)),
            max_iterations=int(self.receptor_max_iterations.value()),
            tolerance_kj_mol=float(self.receptor_tolerance.value()),
            batch_size=int(self.batch_size_receptors.value()),
            executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
        )

    def _save_chem_to_workflow(self, kind: str, label: str, cfg: dict) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        # kind == chemistry API method name; capture cfg now so the deferred submit is widget-free.
        save_to_workflow(
            self.window(), kind=kind, name=label, category="chemistry",
            submit=lambda rt, m=kind, c=cfg: getattr(rt.chemistry, m)(**c),
        )

    def _run_protonate_ligands(self) -> None:
        cfg = self._cfg_protonate_ligands()
        self._submit(
            "Protonate Small Molecules",
            lambda: self.runtime.chemistry.protonate_ligands(**cfg),
        )

    def _save_protonate_ligands(self) -> None:
        self._save_chem_to_workflow(
            "protonate_ligands",
            "Protonate small molecules",
            self._cfg_protonate_ligands(),
        )

    def _run_generate_3d_ligands(self) -> None:
        cfg = self._cfg_generate_3d_ligands()
        self._submit("Generate 3D", lambda: self.runtime.chemistry.generate_3d_ligands(**cfg))

    def _save_generate_3d_ligands(self) -> None:
        self._save_chem_to_workflow(
            "generate_3d_ligands", "Generate 3D", self._cfg_generate_3d_ligands()
        )

    def _run_generate_ligand_conformers(self) -> None:
        cfg = self._cfg_generate_ligand_conformers()
        self._submit(
            "Generate Conformers",
            lambda: self.runtime.chemistry.generate_ligand_conformers(**cfg),
        )

    def _save_generate_ligand_conformers(self) -> None:
        self._save_chem_to_workflow(
            "generate_ligand_conformers",
            "Generate conformers",
            self._cfg_generate_ligand_conformers(),
        )

    def _run_minimize_ligands(self) -> None:
        cfg = self._cfg_minimize_ligands()
        self._submit("Minimize", lambda: self.runtime.chemistry.minimize_ligands(**cfg))

    def _save_minimize_ligands(self) -> None:
        self._save_chem_to_workflow("minimize_ligands", "Minimize", self._cfg_minimize_ligands())

    def _run_protonate_receptors(self) -> None:
        cfg = self._cfg_protonate_receptors()
        method = str(cfg["method"])
        if not self._require_executable(
            title="Protonate Proteins",
            executable=method,
            detail=f"Protein protonation with {method} requires its command-line tool.",
        ):
            return
        self._submit(
            "Protonate Proteins",
            lambda: self.runtime.chemistry.protonate_receptors(**cfg),
        )

    def _save_protonate_receptors(self) -> None:
        self._save_chem_to_workflow(
            "protonate_receptors",
            "Protonate proteins",
            self._cfg_protonate_receptors(),
        )

    def _run_fix_receptors(self) -> None:
        if not self._require_optional_module(
            title="Fix Structure",
            modules=("pdbfixer", "openmm"),
            detail="Fixing a protein structure currently requires PDBFixer and OpenMM in the active environment.",
        ):
            return
        cfg = self._cfg_fix_receptors()
        self._submit("Fix Structure", lambda: self.runtime.chemistry.fix_receptors(**cfg))

    def _save_fix_receptors(self) -> None:
        self._save_chem_to_workflow(
            "fix_receptors", "Fix structure", self._cfg_fix_receptors()
        )

    def _run_minimize_receptors(self) -> None:
        if not self._require_optional_module(
            title="Minimize Proteins",
            modules=("openmm",),
            detail="Protein minimization currently requires OpenMM in the active environment.",
        ):
            return
        cfg = self._cfg_minimize_receptors()
        self._submit("Minimize Proteins", lambda: self.runtime.chemistry.minimize_receptors(**cfg))

    def _save_minimize_receptors(self) -> None:
        self._save_chem_to_workflow(
            "minimize_receptors", "Minimize proteins", self._cfg_minimize_receptors()
        )

    def refresh(self) -> None:
        self._on_scope_changed()
        self._refresh_protonation_tools()


def register_build_workspace(window) -> None:
    window.register_main_view(
        BUILD_ID,
        "Molecule Build",
        lambda: MoleculeBuildWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = [
    "BUILD_ID",
    "MoleculeBuildWidget",
    "register_build_workspace",
]
