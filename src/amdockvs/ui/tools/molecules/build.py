from __future__ import annotations

import importlib.util
import os
import shutil

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from amdockvs.core.user_env import is_remembered, remember_env_var, shell_profile
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.common.job_follower import JobFollower
from amdockvs.ui.common.widgets import RunDestinationCombo, gate_workflow_save
from amdockvs.ui.catalog.molecules import MOLECULES_VIEW_ID
from amdockvs.ui.catalog.shards import SHARDS_VIEW_ID
from amdockvs.core.vocab import MoleculeType
from ms_components.ms_table import FilterOperator, FilterSpec
from ms_components.tool_panel import ToolPanel, format_count, hint_label

BUILD_ID = "moltools.build"
ESM_TOKEN_ENV = "ESM_API_KEY"  # same name as chemistry.tools.esmfold; the UI only sets it


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
        self._library_sharded = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        self.tabs = QTabWidget(self)
        outer.addWidget(self.tabs, 1)

        self.small_molecules_tab = ToolPanel(self.tabs)
        self.proteins_tab = ToolPanel(self.tabs)
        self.tabs.addTab(self.small_molecules_tab, "Small molecules")
        self.tabs.addTab(self.proteins_tab, "Proteins")

        self.batch_size_ligands = _spinbox(minimum=1, maximum=4096, value=128)
        self.batch_size_receptors = _spinbox(minimum=1, maximum=256, value=8)
        small = self.small_molecules_tab
        # Only a sharded project has two places small molecules live; elsewhere the top bar stays hidden.
        self.ligand_target = QComboBox(small)
        self.ligand_target.addItem("Screening library (shards)", "shards")
        self.ligand_target.addItem("Reference molecules (rows)", "rows")
        self.ligand_target.currentIndexChanged.connect(self._on_ligand_target_changed)
        small.top_bar.add_label("Apply to", self.ligand_target)
        small.top_bar.add_widget(self.ligand_target)
        small.top_bar.add_stretch()

        self.ligand_protonation_section = small.add_section("Protonation", checkable=True)
        self.ligand_protonation_method = QComboBox(small)
        self.ligand_protonation_method.addItem("Dimorphite-DL", "dimorphite")
        self.ligand_protonation_method.addItem("OpenBabel", "openbabel")
        self.ligand_protonation_method.addItem("pKasso", "pkasso")
        self.ligand_protonation_method.addItem("Explicit Hs (RDKit)", "explicit_hs")
        # One pH for every pH-aware method; Explicit Hs predicts no states, so it greys the pH out.
        self.ligand_protonation_ph = _double_spinbox(minimum=0.0, maximum=14.0, value=7.4, step=0.1, decimals=1)
        self._protonation_tool_ready = {
            # dimorphite_dl is a declared dependency, but a stale env still ships without it
            "dimorphite": _module_available("dimorphite_dl"),
            "openbabel": False,
            "pkasso": False,
            "explicit_hs": True,
        }
        self.openbabel_status = hint_label("Checking runtime…", small)
        self.pkasso_model = QComboBox(small)
        self.pkasso_model.addItem("MolGpKa (fast)", "molgpka")
        self.pkasso_model.addItem("MolGpKa + Uni-pKa (precise)", "mixed")
        self.pkasso_threads = _spinbox(minimum=1, maximum=128, value=1)
        self.pkasso_gpu = _checkbox("Request one GPU", checked=False)
        self.pkasso_model.currentIndexChanged.connect(self._sync_pkasso_options)
        self.pkasso_status = hint_label("Checking runtime…", small)
        dimorphite_note = hint_label(
            "Rule-based, pH-aware single-state assignment. Ambiguous microstates are not retained.", small
        )
        openbabel_note = hint_label("Fast pH-aware protonation through the OpenBabel command-line tool.", small)
        pkasso_note = hint_label(
            "Selects only the most probable pH-specific state; microstate derivatives are not stored.", small
        )
        explicit_hs_note = hint_label(
            "Adds every hydrogen with RDKit, keeping the current charge states; this is not pH prediction.", small
        )
        protonation = self.ligand_protonation_section
        protonation.add_row("Method", self.ligand_protonation_method)
        protonation.add_row("pH", self.ligand_protonation_ph)
        protonation.add_row(dimorphite_note)
        protonation.add_row(openbabel_note)
        protonation.add_row(self.openbabel_status)
        protonation.add_row(pkasso_note)
        protonation.add_row("Model", self.pkasso_model)
        protonation.add_row("Threads", self.pkasso_threads)
        protonation.add_row(self.pkasso_gpu)
        protonation.add_row(self.pkasso_status)
        protonation.add_row(explicit_hs_note)
        # Each method's rows show only for that method: a stacked page would reserve the tallest
        # method's height under all of them.
        self.ligand_protonation_rows: dict[str, list[QWidget]] = {
            "dimorphite": [dimorphite_note],
            "openbabel": [openbabel_note, self.openbabel_status],
            "pkasso": [pkasso_note, self.pkasso_model, self.pkasso_threads, self.pkasso_gpu, self.pkasso_status],
            "explicit_hs": [explicit_hs_note],
        }

        # Embedding always adds the missing Hs (existing ones are kept), so it is not an option.
        # Geometry cleanup is the Minimization step; 3D only optimizes to rank several attempts.
        self.ligand_3d_section = small.add_section("3D generation", checkable=True)
        self.ligand_embed_method = QComboBox(small)
        self.ligand_embed_method.addItem("ETKDGv3", "etkdgv3")
        self.ligand_embed_method.addItem("srETKDGv3 (small rings)", "sretkdgv3")
        self.ligand_embed_attempts = _spinbox(minimum=1, maximum=50, value=1)
        self.ligand_3d_section.add_row("Method", self.ligand_embed_method)
        self.ligand_3d_section.add_row("Attempts", self.ligand_embed_attempts)
        self.ligand_3d_section.add_row(hint_label(
            "More than one attempt optimizes each (MMFF, UFF fallback) and keeps the lowest energy.", small
        ))

        self.ligand_min_section = small.add_section("Minimization", checkable=True, checked=False)
        self.ligand_forcefield = QComboBox(small)
        self.ligand_forcefield.addItem("MMFF", "mmff")
        self.ligand_forcefield.addItem("UFF", "uff")
        self.ligand_max_iters = _spinbox(minimum=1, maximum=10000, value=200)
        self.ligand_min_section.add_row("Forcefield", self.ligand_forcefield)
        self.ligand_min_section.add_row("Max iterations", self.ligand_max_iters)

        # Shown after Minimization because 3D + minimize is the common build; when both are
        # ticked the pass still runs conformers first and minimizes the ensemble.
        self.ligand_conformers_section = small.add_section(
            "Conformer ensemble",
            checkable=True,
            checked=False,
            description="Uses the random seed. If Minimization is ticked, it runs on every conformer.",
        )
        self.ligand_num_conformers = _spinbox(minimum=1, maximum=500, value=20)
        self.ligand_prune_rms = _double_spinbox(minimum=0.0, maximum=10.0, value=0.5, step=0.1)
        self.ligand_prune_rms.setSuffix(" Å")
        self.ligand_conformers_section.add_row("Num conformers", self.ligand_num_conformers)
        self.ligand_conformers_section.add_row("Prune RMS", self.ligand_prune_rms)

        # One seed for every stochastic stage, so a whole build reproduces from a single number.
        self.ligand_random_seed = _spinbox(minimum=0, maximum=2**31 - 1, value=0xF00D)
        self.ligand_destination = RunDestinationCombo(small, self.runtime)
        small.advanced.add_row("Random seed", self.ligand_random_seed)
        small.advanced.add_row("Batch size", self.batch_size_ligands)
        small.advanced.add_row("Run on", self.ligand_destination)
        small.advanced.form.setRowVisible(self.ligand_destination, self.ligand_destination.count() > 1)

        # Protonation -> 3D -> minimization -> conformers as one pass: each molecule is read and
        # written once, and a sharded library gets one new generation instead of a copy per step.
        build_bar = small.action_bar
        build_bar.add_action(self._workflow_button(small, self._save_build_ligands))
        self.run_build_ligands_button = build_bar.add_action(QPushButton("Run", small))
        self.run_build_ligands_button.clicked.connect(self._run_build_ligands)
        self.ligand_jobs = JobFollower(self, build_bar, noun="Build")
        for section in (
            self.ligand_protonation_section,
            self.ligand_3d_section,
            self.ligand_min_section,
            self.ligand_conformers_section,
        ):
            section.toggled.connect(self._sync_build_button)

        # Predict -> fix -> protonate -> minimize, as one Run: the ticked ops are chained with depends_on.
        proteins = self.proteins_tab
        # Off by default: an external API call. Proteins without 3D get their first model from it.
        self.receptor_predict_section = proteins.add_section("3D generation", checkable=True, checked=False)
        self.receptor_predictor = QComboBox(proteins)
        self.receptor_predictor.addItem("ESMFold2 fast", "esmfold2-fast-2026-05")
        self.receptor_predictor.addItem("ESMFold2", "esmfold2-2026-05")
        self.receptor_predict_force = _checkbox("Force: predict again proteins that already have 3D")
        self.receptor_token_button = QPushButton("API token…", proteins)
        self.receptor_token_button.clicked.connect(self._ask_esm_token)
        self.receptor_predict_section.add_row("Model", self.receptor_predictor)
        self.receptor_predict_section.add_row(self.receptor_predict_force)
        self.receptor_predict_section.add_row(self.receptor_token_button)

        self.receptor_fix_section = proteins.add_section("Fix structure", checkable=True)
        self.fix_missing_residues = _checkbox("Add missing residues", checked=True)
        self.fix_missing_atoms = _checkbox("Add missing atoms", checked=True)
        self.fix_replace_nonstandard = _checkbox("Replace nonstandard residues", checked=True)
        self.fix_remove_heterogens = _checkbox("Remove heterogens", checked=False)
        self.fix_keep_water = _checkbox("Keep water when removing heterogens", checked=True)
        self.receptor_fix_structure = self._structure_combo(proteins)
        self.receptor_fix_section.add_row("Structure", self.receptor_fix_structure)
        for checkbox in (
            self.fix_missing_residues,
            self.fix_missing_atoms,
            self.fix_replace_nonstandard,
            self.fix_remove_heterogens,
            self.fix_keep_water,
        ):
            self.receptor_fix_section.add_row(checkbox)

        self.receptor_protonation_section = proteins.add_section("Protonation", checkable=True)
        self.receptor_protonation_method = QComboBox(proteins)
        self.receptor_protonation_method.addItem("Reduce", "reduce")
        self.receptor_protonation_method.addItem("PDB2PQR", "pdb2pqr")
        self.receptor_protonation_ph = _double_spinbox(
            minimum=0.0, maximum=14.0, value=7.0, step=0.1, decimals=1
        )
        self.receptor_protonation_forcefield = QComboBox(proteins)
        for label in ("AMBER", "CHARMM", "PARSE"):
            self.receptor_protonation_forcefield.addItem(label, label)
        self.receptor_protonation_section.add_row("Method", self.receptor_protonation_method)
        self.receptor_protonation_section.add_row("pH", self.receptor_protonation_ph)
        self.receptor_protonation_section.add_row("Forcefield", self.receptor_protonation_forcefield)

        # Off by default: it moves atoms, and it is the slowest op by far.
        self.receptor_min_section = proteins.add_section("Minimization", checkable=True, checked=False)
        self.receptor_forcefields = QComboBox(proteins)
        self.receptor_forcefields.addItem("amber14-all.xml", ("amber14-all.xml",))
        self.receptor_forcefields.addItem("amber14-all + amber14/tip3p", ("amber14-all.xml", "amber14/tip3p.xml"))
        self.receptor_max_iterations = _spinbox(minimum=1, maximum=100000, value=500)
        self.receptor_tolerance = _double_spinbox(minimum=0.001, maximum=10000.0, value=10.0, step=0.5)
        self.receptor_min_section.add_row("Forcefields", self.receptor_forcefields)
        self.receptor_min_section.add_row("Max iterations", self.receptor_max_iterations)
        self.receptor_min_section.add_row("Tolerance (kJ/mol/nm)", self.receptor_tolerance)

        self.receptor_destination = RunDestinationCombo(proteins, self.runtime)
        proteins.advanced.add_row("Batch size", self.batch_size_receptors)
        proteins.advanced.add_row("Run on", self.receptor_destination)
        proteins.advanced.form.setRowVisible(self.receptor_destination, self.receptor_destination.count() > 1)

        protein_bar = proteins.action_bar
        protein_bar.add_action(self._workflow_button(proteins, self._save_proteins))
        self.run_proteins_button = protein_bar.add_action(QPushButton("Run", proteins))
        self.run_proteins_button.clicked.connect(self._run_proteins)
        self.protein_jobs = JobFollower(self, protein_bar, noun="Protein build")
        for section in self._protein_sections():
            section.toggled.connect(self._sync_protein_button)

        self.receptor_protonation_method.currentIndexChanged.connect(
            self._sync_receptor_protonation_options
        )
        self.ligand_protonation_method.currentIndexChanged.connect(
            self._sync_small_molecule_protonation_options
        )
        self.tabs.currentChanged.connect(self._on_scope_changed)
        self._sync_receptor_protonation_options()
        self._sync_protein_button()
        self._sync_pkasso_options()
        self._sync_small_molecule_protonation_options()
        self._ready = True
        self.refresh()

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

    # --- scope: one molecular type, shown on the Molecules table -------------------
    def _selected_molecule_type(self) -> str:
        if self.tabs.currentWidget() is self.proteins_tab:
            return str(MoleculeType.PROTEIN)
        return str(MoleculeType.SMALL_MOLECULE)

    def _scope(self, molecule_type: str | None = None):
        """What the ops run on: this type, narrowed to whatever the Molecules table shows."""
        resolved_type = str(molecule_type or self._selected_molecule_type())
        scope = self.runtime.molecules.select(molecule_type=resolved_type)
        widget = self._catalog_molecules_widget()
        ids = widget.scope_ids() if widget is not None else None
        if ids is None:
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

    def _on_scope_changed(self, *_args) -> None:
        self._sync_molecules_scope()
        self._refresh_protein_summary()

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
        for name, widgets in self.ligand_protonation_rows.items():
            for widget in widgets:
                self.ligand_protonation_section.form.setRowVisible(widget, name == method)
        self.ligand_protonation_ph.setEnabled(method != "explicit_hs")
        self._sync_build_button()

    def _sync_build_button(self, *_args) -> None:
        steps = self._build_steps()
        method = self._active_protonation_method()
        blocked = self.ligand_protonation_section.isChecked() and not self._protonation_tool_ready.get(method, False)
        self.run_build_ligands_button.setEnabled(bool(steps) and not blocked)
        self.run_build_ligands_button.setToolTip(
            f"{method} is not available in this environment." if blocked
            else "" if steps else "Tick at least one step."
        )

    def _protein_sections(self):
        return self.receptor_predict_section, self.receptor_fix_section, self.receptor_protonation_section, self.receptor_min_section

    def _sync_protein_button(self, *_args) -> None:
        ticked = any(section.isChecked() for section in self._protein_sections())
        self.run_proteins_button.setEnabled(ticked)
        self.run_proteins_button.setToolTip("" if ticked else "Tick at least one step.")

    # --- where small molecules live: rows everywhere, shards too in a sharded project ---
    def _ligands_go_to_shards(self) -> bool:
        return self._library_sharded and self.ligand_target.currentData() == "shards"

    def _ligand_view_id(self) -> str:
        on_small = self.tabs.currentWidget() is self.small_molecules_tab
        return SHARDS_VIEW_ID if on_small and self._ligands_go_to_shards() else MOLECULES_VIEW_ID

    def _sync_ligand_target(self) -> None:
        try:
            self._library_sharded, ligand_rows = self.runtime.molecules.library_shape()
        except Exception:  # noqa: BLE001 - no project open yet
            self._library_sharded, ligand_rows = False, 0
        # Rows are only a choice when there are some: a sharded project may hold none yet.
        self.ligand_target.model().item(1).setEnabled(bool(ligand_rows))
        self.ligand_target.setToolTip("" if ligand_rows else "No reference molecules yet: import some as rows.")
        if not ligand_rows and self.ligand_target.currentData() == "rows":
            self.ligand_target.blockSignals(True)
            self.ligand_target.setCurrentIndex(0)
            self.ligand_target.blockSignals(False)
        shards = self._ligands_go_to_shards()
        self.small_molecules_tab.top_bar.setVisible(self._library_sharded)
        # A shard is already the unit of work (one per task), so a batch size means nothing there.
        self.small_molecules_tab.advanced.form.setRowVisible(self.batch_size_ligands, not shards)
        # One shard record stores one structure: ensembles only exist for rows.
        self.ligand_conformers_section.setEnabled(not shards)
        self.ligand_conformers_section.setToolTip(
            "Conformer ensembles need rows: apply to Reference molecules." if shards else ""
        )
        self._sync_build_button()
        self._refresh_build_summary()

    def _refresh_build_summary(self) -> None:
        """Idle line of the action bar: how much a Run would cover. Counted off the GUI thread."""
        bar = self.small_molecules_tab.action_bar
        if bar.is_running:
            return
        shards = self._ligands_go_to_shards()
        molecules = self.runtime.molecules
        try:
            scope = None if shards else self._scope(MoleculeType.SMALL_MOLECULE)  # reads the table: GUI thread
        except Exception:  # noqa: BLE001 - no project open yet
            bar.set_summary("")
            return

        def count() -> str:
            if shards:
                counts = molecules.shard_counts()
                return f"{format_count(counts['records'])} molecules in {format_count(counts['shards'])} shards"
            return f"{format_count(molecules.count(scope))} molecules"

        self._summarize(bar, count)

    def _refresh_protein_summary(self) -> None:
        bar = self.proteins_tab.action_bar
        if bar.is_running:
            return
        try:
            scope = self._scope(MoleculeType.PROTEIN)
        except Exception:  # noqa: BLE001 - no project open yet
            bar.set_summary("")
            return
        molecules = self.runtime.molecules
        self._summarize(bar, lambda: f"{format_count(molecules.count(scope))} proteins")

    @staticmethod
    def _summarize(bar, text) -> None:
        """Counts off the GUI thread, then shows them unless a Run started meanwhile."""

        def show(value: str) -> None:
            if not bar.is_running:
                bar.set_summary(value)

        run_async(text, show, on_error=lambda _exc: show(""), compact=True)

    def _on_ligand_target_changed(self, *_args) -> None:
        self._sync_ligand_target()
        opener = getattr(self.window(), "open_or_focus_view", None)
        if self.isVisible() and callable(opener):
            opener(self._ligand_view_id())

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
        self._sync_ligand_target()
        self._refresh_protein_summary()
        opener = getattr(self.window(), "open_or_focus_view", None)
        if callable(opener):
            opener(self._ligand_view_id())
        self._sync_molecules_scope()

    def hideEvent(self, event):
        super().hideEvent(event)
        widget = self._catalog_molecules_widget() if self._ready else None
        if widget is not None:
            widget.pop_scope(self._SCOPE_KEY)

    @staticmethod
    def _workflow_button(parent, handler) -> QPushButton:
        button = QPushButton("Save to workflow", parent)
        button.setToolTip("Add this op (current settings) as a workflow step — updates it if already there.")
        button.clicked.connect(handler)
        gate_workflow_save(button)
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
        return False

    def _require_executable(self, *, title: str, executable: str, detail: str) -> bool:
        if _executable_available(executable):
            return True
        QMessageBox.warning(
            self,
            title,
            f"{detail}\nMissing executable: {executable}",
        )
        return False

    # Each op exposes its CURRENT config as a dict (kind == chemistry API method name), so Run and
    # Save-to-workflow share one source of truth and stay in sync.
    def _build_steps(self) -> list[tuple[str, dict]]:
        """The ticked sections, always in the order protonate -> 3D -> conformers -> minimize."""
        steps: list[tuple[str, dict]] = []
        seed = int(self.ligand_random_seed.value())
        if self.ligand_protonation_section.isChecked():
            method = self._active_protonation_method()
            steps.append(("protonate", dict(
                method=method,
                ph=float(self.ligand_protonation_ph.value()),
                model=str(self.pkasso_model.currentData() or "molgpka"),
                threads=int(self.pkasso_threads.value()),
                gpu=self.pkasso_gpu.isChecked(),
            )))
        if self.ligand_3d_section.isChecked():
            # Same fragment/metal/ion settings as generate_3d_ligands(): 3D keeps what import kept.
            steps.append(("generate_3d", dict(
                random_seed=seed,
                method=str(self.ligand_embed_method.currentData() or "etkdgv3"),
                attempts=int(self.ligand_embed_attempts.value()),
                fragment_mode="keep",
                filter_metals=False,
                filter_simple_ions=False,
            )))
        # run_shard_pipeline refuses ensembles, and the section is disabled for shards anyway.
        if self.ligand_conformers_section.isChecked() and not self._ligands_go_to_shards():
            steps.append(("conformers", dict(
                num_conformers=int(self.ligand_num_conformers.value()),
                random_seed=seed,
                prune_rms_thresh=float(self.ligand_prune_rms.value()),
                optimize=False,  # geometry cleanup is the Minimization step below
            )))
        if self.ligand_min_section.isChecked():
            ensemble = any(name == "conformers" for name, _ in steps)
            steps.append(("minimize", dict(
                forcefield=str(self.ligand_forcefield.currentData() or "mmff"),
                max_iters=int(self.ligand_max_iters.value()),
                # Minimized conformers can converge; prune again with the ensemble's own threshold.
                prune_rms_thresh=float(self.ligand_prune_rms.value()) if ensemble else 0.0,
            )))
        return steps

    def _cfg_build_ligands(self) -> dict:
        # ligands=None is how run_ligand_pipeline picks the shards; a row scope always means rows.
        return dict(
            steps=self._build_steps(),
            ligands=None if self._ligands_go_to_shards() else self._scope(MoleculeType.SMALL_MOLECULE),
            batch_size=int(self.batch_size_ligands.value()),
            executor_name=self.ligand_destination.executor_name(),
        )

    def _cfg_protonate_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            method=str(self.receptor_protonation_method.currentData() or "reduce"),
            ph=float(self.receptor_protonation_ph.value()),
            forcefield=str(self.receptor_protonation_forcefield.currentData() or "AMBER"),
            batch_size=int(self.batch_size_receptors.value()),
            executor_name=self.receptor_destination.executor_name(),
        )

    def _cfg_predict_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            model=str(self.receptor_predictor.currentData() or "esmfold2-fast-2026-05"),
            force=self.receptor_predict_force.isChecked(),
        )

    def _ask_esm_token(self) -> bool:
        """Sets ESM_API_KEY in this process. AMDock never stores the token; "Remember" writes the
        export line to the user's own shell profile (user environment on Windows), on request."""
        remembered = is_remembered(ESM_TOKEN_ENV)
        target = "your user environment" if os.name == "nt" else str(shell_profile())
        dialog = QDialog(self)
        dialog.setWindowTitle("ESMFold API token")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Paste your Biohub API token (https://biohub.ai).\n"
            "AMDock keeps it in memory for this session and never saves it.",
            dialog,
        ))
        field = QLineEdit(os.environ.get(ESM_TOKEN_ENV, ""), dialog)
        field.setEchoMode(QLineEdit.Password)
        layout.addWidget(field)
        remember = QCheckBox(f"Remember: add export {ESM_TOKEN_ENV}=… to {target}", dialog)
        remember.setChecked(remembered)
        layout.addWidget(remember)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            token = field.text().strip()
            if token:
                os.environ[ESM_TOKEN_ENV] = token
            else:
                os.environ.pop(ESM_TOKEN_ENV, None)
            try:
                if remember.isChecked() and token:
                    remember_env_var(ESM_TOKEN_ENV, token)
                elif remembered:  # unticked or cleared: take the export line out again
                    remember_env_var(ESM_TOKEN_ENV, "")
            except OSError as exc:
                QMessageBox.warning(self, "ESMFold API token", f"Could not update {target}:\n{exc}")
        return bool(os.environ.get(ESM_TOKEN_ENV, "").strip())

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
            executor_name=self.receptor_destination.executor_name(),
        )

    def _cfg_minimize_receptors(self) -> dict:
        return dict(
            receptors=self._scope(MoleculeType.PROTEIN),
            forcefields=tuple(self.receptor_forcefields.currentData() or ("amber14-all.xml",)),
            max_iterations=int(self.receptor_max_iterations.value()),
            tolerance_kj_mol=float(self.receptor_tolerance.value()),
            batch_size=int(self.batch_size_receptors.value()),
            executor_name=self.receptor_destination.executor_name(),
        )

    def _save_chem_to_workflow(self, kind: str, label: str, cfg: dict) -> None:
        from amdockvs.ui.tools.workflow_panel import save_to_workflow

        # kind == chemistry API method name; capture cfg now so the deferred submit is widget-free.
        save_to_workflow(
            self.window(), kind=kind, name=label, category="chemistry",
            submit=lambda rt, m=kind, c=cfg: getattr(rt.chemistry, m)(**c),
        )

    def _run_build_ligands(self) -> None:
        cfg = self._cfg_build_ligands()
        try:
            job_id = self.runtime.chemistry.run_ligand_pipeline(**cfg)
        except Exception as exc:  # noqa: BLE001 - shown to the user, nothing was submitted
            QMessageBox.critical(self, "Build Small Molecules", str(exc))
            return
        # No "submitted" popup: the action bar carries the run, the shell toast announces it.
        self.ligand_jobs.follow([("Building", job_id)], unit="shards" if cfg["ligands"] is None else "batches")

    def _save_build_ligands(self) -> None:
        self._save_chem_to_workflow(
            "run_ligand_pipeline", "Build small molecules", self._cfg_build_ligands()
        )

    # --- proteins: the ticked ops, chained ------------------------------------------------
    def _protein_ops(self) -> list[tuple[str, str, str, dict]]:
        """(chemistry API method, bar stage, workflow step name, config) in pipeline order."""
        ops = []
        if self.receptor_predict_section.isChecked():
            ops.append(("predict_receptors", "Predicting", "Predict structures", self._cfg_predict_receptors()))
        if self.receptor_fix_section.isChecked():
            ops.append(("fix_receptors", "Fixing", "Fix structure", self._cfg_fix_receptors()))
        if self.receptor_protonation_section.isChecked():
            ops.append(("protonate_receptors", "Protonating", "Protonate proteins", self._cfg_protonate_receptors()))
        if self.receptor_min_section.isChecked():
            ops.append(("minimize_receptors", "Minimizing", "Minimize proteins", self._cfg_minimize_receptors()))
        return ops

    def _protein_tools_ready(self, ops) -> bool:
        """Every ticked op's tool, checked before anything is submitted."""
        for kind, _stage, _name, cfg in ops:
            if kind == "predict_receptors" and not os.environ.get(ESM_TOKEN_ENV, "").strip() and not self._ask_esm_token():
                return False
            if kind == "fix_receptors" and not self._require_optional_module(
                title="Fix structure",
                modules=("pdbfixer", "openmm"),
                detail="Fixing a protein structure currently requires PDBFixer and OpenMM in the active environment.",
            ):
                return False
            if kind == "protonate_receptors" and not self._require_executable(
                title="Protonation",
                executable=cfg["method"],
                detail=f"Protein protonation with {cfg['method']} requires its command-line tool.",
            ):
                return False
            if kind == "minimize_receptors" and not self._require_optional_module(
                title="Minimization",
                modules=("openmm",),
                detail="Protein minimization currently requires OpenMM in the active environment.",
            ):
                return False
        return True

    def _run_proteins(self) -> None:
        ops = self._protein_ops()
        if not ops or not self._protein_tools_ready(ops):
            return
        stages, depends_on, error = [], None, None
        try:
            for kind, stage, _name, cfg in ops:
                # Chemistry jobs do not wait for each other on their own: chain them explicitly.
                job_id = str(getattr(self.runtime.chemistry, kind)(**cfg, depends_on=depends_on))
                stages.append((stage, job_id))
                depends_on = [job_id]
        except Exception as exc:  # noqa: BLE001 - shown below; what was submitted is still followed
            error = exc
        if stages:
            self.protein_jobs.follow(stages)
        if error is not None:
            QMessageBox.critical(self, "Build Proteins", str(error))

    def _save_proteins(self) -> None:
        # One step per op: the workflow already runs same-category steps one after another.
        for kind, _stage, name, cfg in self._protein_ops():
            self._save_chem_to_workflow(kind, name, cfg)

    def refresh(self) -> None:
        self._sync_ligand_target()
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
