from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from amdockvs.configuration import app_config
from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    build_receptor_import_preview,
    scan_receptor_structure,
)
from amdockvs.molecule_paths import current_molecule_path
from amdockvs.models import MoleculeRecord
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.drop_area import TablePlaceholder, drop_hint, icon_button
from amdockvs.ui.resources.icons import icon
from amdockvs.ui.tools.pymol_ribbon import (
    apply_receptor_atom_coloring,
    set_pymol_scene_context,
)
from amdockvs.vocab import FileFormat, MoleculeUsageClass
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
    ToolbarAction,
    choices_from_class,
)

RECEPTOR_VIEW_ID = "workspace.receptors"
RECEPTOR_PREVIEW_MAX_FILES = 50


def _as_ref_tuple(value) -> tuple | None:
    """None = 'all cocrystal ligands' (default); a tuple = exactly those selectors."""
    return None if value is None else tuple(value)


class _ReceptorScanWorker(QObject):
    progress = Signal(int, int, str, dict)
    finished = Signal()

    def __init__(self, *, file_paths: list[str]):
        super().__init__()
        self._file_paths = list(file_paths)

    @Slot()
    def run(self) -> None:
        total = len(self._file_paths)
        for index, path in enumerate(self._file_paths, start=1):
            self.progress.emit(index, total, path, scan_receptor_structure(path))
        self.finished.emit()


class _ChipsSelect(QWidget):
    """Generic chips + combo multi-select (same shape as ms_table's Order/Filter bars): the combo
    offers the not-yet-picked items, each pick becomes a removable chip. Used for both the chain
    selection and the reference-ligand selection.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._chips_layout = QHBoxLayout()
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(3)
        layout.addLayout(self._chips_layout)
        layout.addStretch(1)  # push the (small) add-combo to the right
        self._combo = QComboBox(self)
        self._combo.setFixedWidth(46)
        self._combo.currentIndexChanged.connect(self._on_add)
        layout.addWidget(self._combo)
        self._options: list[str] = []
        self._selected: list[str] = []

    def set_items(self, items, *, selected=None) -> None:
        self._options = [str(item) for item in items]
        if selected is None:
            self._selected = list(self._options)
        else:
            self._selected = [str(item) for item in selected if str(item) in self._options]
        self._rebuild()

    def selected_items(self) -> list[str]:
        return list(self._selected)

    def _rebuild(self) -> None:
        while self._chips_layout.count():
            widget = self._chips_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for item in self._selected:
            self._chips_layout.addWidget(self._make_chip(item))
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("＋", None)
        for option in self._options:
            if option not in self._selected:
                self._combo.addItem(option, option)
        self._combo.setCurrentIndex(0)
        self._combo.setEnabled(self._combo.count() > 1)
        self._combo.blockSignals(False)

    def _make_chip(self, item: str) -> QFrame:
        chip = QFrame(self)
        chip.setProperty("chip_objet", True)
        chip.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(chip)
        row.setContentsMargins(6, 0, 2, 0)
        row.setSpacing(2)
        row.addWidget(QLabel(item, chip))
        remove = QToolButton(chip)
        remove.setText("×")
        remove.setAutoRaise(True)
        remove.clicked.connect(lambda: self._remove(item))
        row.addWidget(remove)
        return chip

    def _on_add(self, index: int) -> None:
        item = self._combo.itemData(index)
        if item is None or item in self._selected:
            return
        self._selected.append(item)
        self._rebuild()
        self.changed.emit()

    def _remove(self, item: str) -> None:
        if item in self._selected:
            self._selected.remove(item)
            self._rebuild()
            self.changed.emit()


class ReceptorImportPanel(QWidget):
    """Non-modal receptor import UI: options + per-file preview table (scan on a worker thread).

    Hosted by the dedicated Import workspace tab. Emits ready_changed(bool) when the scan
    finishes so the host can gate its "Queue Import" button. File-set edits are only allowed
    when no scan is running (the Add/Remove/Clear buttons are disabled during a scan).
    """

    ready_changed = Signal(bool)

    def __init__(
        self,
        *,
        runtime=None,  # only to read settings (defaults seeded below); the host submits the job
        file_paths: list[str] | None = None,
        show_file_controls: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._runtime = runtime
        self._show_file_controls = bool(show_file_controls)
        # Drag-and-drop feeds the same add path as the button, when file editing is available.
        self.setAcceptDrops(self._show_file_controls)
        self._ready = False
        self._file_paths = [str(Path(path).expanduser().resolve()) for path in (file_paths or [])]
        self._enable_preview = len(self._file_paths) <= RECEPTOR_PREVIEW_MAX_FILES
        self._previews_by_path: dict[str, dict] = {}
        self._scan_cache_by_path: dict[str, dict] = {}
        self._row_state_by_path: dict[str, dict[str, str]] = {}
        self._unit_by_path: dict[str, str] = {}  # last-applied assembly per file, to reset chains on unit change
        self._ligand_sig_by_path: dict[str, tuple] = {}  # (assembly, chains) sig, to reset ligand picks when it changes
        self._refreshing = False
        self._scan_complete = not self._enable_preview
        self._scan_thread: QThread | None = None
        self._scan_worker: _ReceptorScanWorker | None = None

        root = QVBoxLayout(self)
        options_layout = QHBoxLayout()
        root.addLayout(options_layout)

        options_form = QFormLayout()
        options_layout.addLayout(options_form, 2)

        # Docking mode (docking / redocking / rescoring) is chosen in the Docking Studio, not here.
        # At import, every cocrystal ligand is registered as a reference automatically.

        # Assembly choice is per-row now (the "Unit" column), so no global bioassembly toggle.
        # Single-arg addRow spans both columns so the checkboxes sit flush left (no empty label gap).
        self.remove_non_structural_waters = QCheckBox("Remove non-structural waters", self)
        self.remove_non_structural_waters.setChecked(True)
        options_form.addRow(self.remove_non_structural_waters)

        self.create_binding_sites = QCheckBox("Create binding sites from ligands and coordinated metals", self)
        self.create_binding_sites.setChecked(True)
        options_form.addRow(self.create_binding_sites)

        self.remove_cofactors = QCheckBox("Remove cofactors during preparation", self)
        options_form.addRow(self.remove_cofactors)

        self.remove_altloc = QCheckBox("Resolve AltLoc to A / highest occupancy", self)
        self.remove_altloc.setChecked(True)
        options_form.addRow(self.remove_altloc)

        self.binding_site_box_size = QDoubleSpinBox(self)
        self.binding_site_box_size.setRange(8.0, 40.0)
        self.binding_site_box_size.setDecimals(1)
        self.binding_site_box_size.setSingleStep(1.0)
        self.binding_site_box_size.setValue(app_config(self._runtime).docking.binding_site_box_size)
        options_form.addRow("Binding Site Box (A)", self.binding_site_box_size)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        options_layout.addWidget(self.status_label, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, max(1, len(self._file_paths)))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(self._enable_preview)
        root.addWidget(self.progress_bar)

        self.table = QTableWidget(0, 10, self)
        self.table.setHorizontalHeaderLabels(
            ["File", "Unit", "Chains", "Ligands", "Metals", "Waters", "Status", "Selected Ligand", "Activity", "As Ligand"]
        )
        header = self.table.horizontalHeader()
        # Every column is user-resizable (Interactive) with a sensible default width; Chains needs
        # room for its chips+combo so it isn't clipped.
        header.setStretchLastSection(False)
        default_column_widths = {0: 170, 1: 90, 2: 160, 3: 210, 4: 90, 5: 60, 6: 80, 7: 160, 8: 120, 9: 80}
        for column, width in default_column_widths.items():
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.table.setColumnWidth(column, width)
        # Selected Ligand (7) / Activity (8) belong to redocking/rescoring, chosen in Docking Studio.
        self.table.setColumnHidden(7, True)
        self.table.setColumnHidden(8, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setVisible(self._enable_preview)

        # Same shape as the ligand importer: table + vertical add/remove toolbar, with a drop hint.
        table_row = QHBoxLayout()
        table_row.addWidget(self.table, 1)
        if self._show_file_controls:
            self._placeholder = TablePlaceholder(self.table, drop_hint("receptor"))
            toolbar = QVBoxLayout()
            self.add_files_button = icon_button(self, "file-plus.svg", "Add files")
            self.remove_files_button = icon_button(self, "shredder.svg", "Remove selected files")
            self.add_files_button.clicked.connect(self._on_add_files)
            self.remove_files_button.clicked.connect(self._on_remove_selected_files)
            for button in (self.add_files_button, self.remove_files_button):
                toolbar.addWidget(button)
            toolbar.addStretch(1)
            table_row.addLayout(toolbar)
        root.addLayout(table_row, 1)

        self.info_label = QLabel(self)
        self.info_label.setWordWrap(True)
        self.info_label.setVisible(not self._enable_preview)
        if not self._enable_preview:
            self.info_label.setText(
                f"{len(self._file_paths)} receptors selected. Preview is disabled above {RECEPTOR_PREVIEW_MAX_FILES} files. "
                "The import job will be submitted directly."
            )
        root.addWidget(self.info_label)

        self.remove_non_structural_waters.toggled.connect(self.refresh_preview)
        self.create_binding_sites.toggled.connect(self.refresh_preview)
        self.remove_cofactors.toggled.connect(self.refresh_preview)
        self.remove_altloc.toggled.connect(self.refresh_preview)
        self.binding_site_box_size.valueChanged.connect(self.refresh_preview)

        self.set_files(self._file_paths)

    # ---- file set management -------------------------------------------------
    def set_files(self, paths) -> None:
        self._file_paths = [str(Path(p).expanduser().resolve()) for p in (paths or [])]
        self._enable_preview = len(self._file_paths) <= RECEPTOR_PREVIEW_MAX_FILES
        self._previews_by_path.clear()
        self._scan_cache_by_path.clear()
        self._row_state_by_path.clear()
        self._scan_thread = None
        self._scan_worker = None
        self._scan_complete = not self._enable_preview
        self.progress_bar.setRange(0, max(1, len(self._file_paths)))
        self.progress_bar.setValue(0)
        self._apply_preview_visibility()
        self._emit_ready(False)
        if not self._file_paths:
            self.table.setRowCount(0)
            self.status_label.setText("No receptor files selected.")
            self._sync_file_controls()
            return
        if self._enable_preview:
            self._prime_table()
            self._apply_mode_visibility()
            self._sync_file_controls()
            QTimer.singleShot(0, self.start_scan)
        else:
            self.table.setRowCount(0)
            self._apply_mode_visibility()
            self.refresh_preview()
            self._emit_ready(True)
            self._sync_file_controls()

    @property
    def file_paths(self) -> list[str]:
        return list(self._file_paths)

    def ligand_role_files(self) -> list[str]:
        """Receptor files the user also flagged as general ligands (dimer case)."""
        # ponytail: only the preview table carries the checkboxes; the no-preview path
        # (huge receptor batches) has none, which is fine — that path is receptor-only.
        marked: list[str] = []
        for row, path in enumerate(self._file_paths):
            widget = self.table.cellWidget(row, 9)
            if isinstance(widget, QCheckBox) and widget.isChecked():
                marked.append(path)
        return marked

    def is_ready(self) -> bool:
        return bool(self._ready) and bool(self._file_paths)

    def _emit_ready(self, ready: bool) -> None:
        self._ready = bool(ready)
        self.ready_changed.emit(self._ready)

    def _apply_preview_visibility(self) -> None:
        self.progress_bar.setVisible(self._enable_preview and bool(self._file_paths))
        self.table.setVisible(self._enable_preview)
        self.info_label.setVisible(not self._enable_preview and bool(self._file_paths))
        if not self._enable_preview and self._file_paths:
            self.info_label.setText(
                f"{len(self._file_paths)} receptors selected. Preview is disabled above "
                f"{RECEPTOR_PREVIEW_MAX_FILES} files. The import job will be submitted directly."
            )

    def _sync_file_controls(self) -> None:
        if not self._show_file_controls:
            return
        can_edit = self._scan_complete or not self._file_paths
        self.add_files_button.setEnabled(can_edit)
        self.remove_files_button.setEnabled(can_edit and self.table.rowCount() > 0)

    def _on_add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Receptor Files",
            "",
            "Molecule files (*.sdf *.smi *.smiles *.txt *.mol2 *.pdb *.pdbqt);;All files (*)",
        )
        self._add_paths(paths)

    def _add_paths(self, paths) -> None:
        resolved = [str(Path(p).expanduser().resolve()) for p in (paths or []) if p]
        if resolved:
            self.set_files(list(dict.fromkeys(self._file_paths + resolved)))

    def _can_edit_files(self) -> bool:
        return bool(self._show_file_controls) and (self._scan_complete or not self._file_paths)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and self._can_edit_files():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and self._can_edit_files():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not self._can_edit_files():
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self._add_paths(paths)
            event.acceptProposedAction()

    def _on_remove_selected_files(self) -> None:
        if not self._can_edit_files():
            return
        rows = sorted({index.row() for index in self.table.selectionModel().selectedIndexes()}, reverse=True)
        rows = [r for r in rows if 0 <= r < len(self._file_paths)]
        if not rows:
            return
        # Drop only the selected rows in place. removeRow keeps the other rows' cell widgets
        # (so their ligand/chain picks survive) and the by-path caches stay valid — no re-scan,
        # no lost selections. set_files() would nuke every cache and rescan everything.
        removed_paths = {self._file_paths[r] for r in rows}
        for r in rows:
            self.table.removeRow(r)
        self._file_paths = [p for p in self._file_paths if p not in removed_paths]
        for path in removed_paths:
            for cache in (
                self._previews_by_path,
                self._scan_cache_by_path,
                self._row_state_by_path,
                self._unit_by_path,
                self._ligand_sig_by_path,
            ):
                cache.pop(path, None)
        if not self._file_paths:
            self.set_files([])
            return
        self.progress_bar.setRange(0, max(1, len(self._file_paths)))
        self.progress_bar.setValue(len(self._file_paths))
        self._sync_file_controls()
        self.refresh_preview()

    def import_request(self) -> dict:
        """Raw import intent for rt.loader.load_receptors — the panel only forwards its selections;
        the API owns the scan/preview/spec assembly (see io.receptor_preview.build_receptor_import_maps).
        build_specs is False for large imports (no per-file preview), where the worker preps each file."""
        if self._enable_preview:
            self._capture_row_state()
        base = self._base_options()
        return {
            "binding_site_box_size": base.binding_site_box_size,
            "remove_non_structural_waters": base.remove_non_structural_waters,
            "create_binding_sites": base.create_binding_sites_from_components,
            "remove_cofactors": base.remove_cofactors,
            "remove_altloc": base.remove_altloc,
            "use_biological_assembly": base.use_biological_assembly,
            "import_mode": base.import_mode,
            "per_file": dict(self._row_state_by_path) if self._enable_preview else {},
            "scans": dict(self._scan_cache_by_path) if self._enable_preview else {},
            "build_specs": bool(self._enable_preview),
        }

    def _base_options(self) -> ReceptorImportOptions:
        return ReceptorImportOptions(
            use_biological_assembly=False,  # the per-row Unit column drives assembly selection now
            remove_non_structural_waters=self.remove_non_structural_waters.isChecked(),
            create_binding_sites_from_components=self.create_binding_sites.isChecked(),
            remove_cofactors=self.remove_cofactors.isChecked(),
            remove_altloc=self.remove_altloc.isChecked(),
            import_mode="receptor",
            binding_site_box_size=float(self.binding_site_box_size.value()),
        )

    def _capture_row_state(self) -> None:
        state_by_path: dict[str, dict] = {}
        for row_index, path in enumerate(self._file_paths):
            selected_ligand = ""
            activity = ""
            selected_chains: list[str] = []
            selected_assembly = ""
            unit_widget = self.table.cellWidget(row_index, 1)
            chain_widget = self.table.cellWidget(row_index, 2)
            ligand_widget = self.table.cellWidget(row_index, 7)
            activity_widget = self.table.cellWidget(row_index, 8)
            if isinstance(unit_widget, QComboBox) and unit_widget.count():
                selected_assembly = str(unit_widget.currentData() or "")
            else:
                # First pass (combo not populated yet): default to the first biological assembly.
                assemblies = list((self._scan_cache_by_path.get(path) or {}).get("assemblies") or [])
                selected_assembly = str(assemblies[0]) if assemblies else ""
            if isinstance(chain_widget, _ChipsSelect):
                selected_chains = chain_widget.selected_items()
            # Changing the unit resets the chain selection to that unit's chains (stale cross-unit
            # picks would otherwise wrongly restrict the new unit).
            if self._unit_by_path.get(path) not in (None, selected_assembly):
                selected_chains = []
            self._unit_by_path[path] = selected_assembly

            # Reference ligands: None means "all candidates" (default). Reset to None whenever the
            # candidate set changes (unit/chain change) or on the first pass; otherwise keep the pick.
            # The signature uses the effective kept chains (stable even before the chip widgets are
            # populated), so a stable unit/chain state doesn't spuriously reset the ligand picks.
            scan = self._scan_cache_by_path.get(path) or {}
            assembly_chains = scan.get("assembly_chains") or {}
            all_labels = [str(chain.get("label")) for chain in (scan.get("chains") or [])]
            if selected_assembly and selected_assembly in assembly_chains:
                unit_chains = [label for label in all_labels if label in set(assembly_chains[selected_assembly])]
            else:
                unit_chains = list(all_labels)
            effective_chains = [c for c in selected_chains if c in unit_chains] if selected_chains else list(unit_chains)
            selected_reference_ligands: list[str] | None = None
            ligand_chips = self.table.cellWidget(row_index, 3)
            sig = (selected_assembly, tuple(sorted(effective_chains)))
            last_sig = self._ligand_sig_by_path.get(path)
            self._ligand_sig_by_path[path] = sig
            if last_sig is not None and last_sig == sig and isinstance(ligand_chips, _ChipsSelect):
                selected_reference_ligands = ligand_chips.selected_items()

            if isinstance(ligand_widget, QComboBox):
                selected_ligand = str(ligand_widget.currentData() or "")
            if isinstance(activity_widget, QLineEdit):
                activity = str(activity_widget.text() or "").strip()
            state_by_path[path] = {
                "selected_cocrystal_key": selected_ligand,
                "activity": activity,
                "selected_chain_ids": selected_chains,
                "selected_assembly": selected_assembly,
                "selected_reference_ligands": selected_reference_ligands,
            }
        self._row_state_by_path = state_by_path

    def _ligand_widget(self, row_index: int) -> QComboBox | None:
        widget = self.table.cellWidget(row_index, 7)
        return widget if isinstance(widget, QComboBox) else None

    def _activity_widget(self, row_index: int) -> QLineEdit | None:
        widget = self.table.cellWidget(row_index, 8)
        return widget if isinstance(widget, QLineEdit) else None

    def refresh_preview(self) -> None:
        if not self._enable_preview:
            mode = "receptor"
            self.status_label.setText(
                f"{len(self._file_paths)} receptor(s) | mode: {mode} | preview skipped"
            )
            return
        if not self._scan_complete:
            self._apply_mode_visibility()
            self.status_label.setText(
                f"{len(self._scan_cache_by_path)}/{len(self._file_paths)} receptor(s) processed..."
            )
            return
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._capture_row_state()
            self._previews_by_path.clear()
            mode = "receptor"
            review_count = 0
            candidate_count = 0
            for row_index, path in enumerate(self._file_paths):
                scan = self._scan_cache_by_path.get(path)
                if scan is None:
                    continue
                state = self._row_state_by_path.get(path, {})
                base_options = self._base_options()
                options = ReceptorImportOptions(
                    **{
                        **base_options.__dict__,
                        "selected_cocrystal_key": state.get("selected_cocrystal_key", ""),
                        "activity_text": state.get("activity", ""),
                        "selected_chain_ids": tuple(state.get("selected_chain_ids") or ()),
                        "selected_assembly": state.get("selected_assembly", ""),
                        "selected_reference_ligands": _as_ref_tuple(state.get("selected_reference_ligands")),
                    }
                )
                preview = build_receptor_import_preview(scan, options)
                self._previews_by_path[path] = preview
                review_count, candidate_count = self._update_row_display(
                    row_index=row_index,
                    path=path,
                    preview=preview,
                    mode=mode,
                    review_count=review_count,
                    candidate_count=candidate_count,
                    build_widgets=True,
                )

            self._apply_mode_visibility()
            self.status_label.setText(
                f"{len(self._scan_cache_by_path)}/{len(self._file_paths)} receptor(s) processed | ligand candidates in {candidate_count} file(s) | review: {review_count}"
            )
        finally:
            self._refreshing = False

    def _prime_table(self) -> None:
        self.table.setRowCount(len(self._file_paths))
        for row_index, path in enumerate(self._file_paths):
            self._set_item(row_index, 0, Path(path).name)
            for column in (4, 5, 6):  # 1 (Unit), 2 (Chains), 3 (Ligands) are widgets, filled after scan
                self._set_item(row_index, column, "...")
            # Unit (col 1): asymmetric unit vs a biological assembly, populated once the scan knows them.
            unit_combo = QComboBox(self.table)
            unit_combo.currentIndexChanged.connect(self.refresh_preview)
            self.table.setCellWidget(row_index, 1, unit_combo)
            # Chains (col 2) is a per-row multi-select, populated once the scan knows the chains.
            chain_button = _ChipsSelect(self.table)
            chain_button.changed.connect(self.refresh_preview)
            self.table.setCellWidget(row_index, 2, chain_button)
            # Ligands (col 3): pick which cocrystal ligands become references (drop artifacts).
            ligand_chips = _ChipsSelect(self.table)
            ligand_chips.changed.connect(self.refresh_preview)
            self.table.setCellWidget(row_index, 3, ligand_chips)
            ligand_combo = QComboBox(self.table)
            ligand_combo.addItem("None", "")
            self.table.setCellWidget(row_index, 7, ligand_combo)
            activity_edit = QLineEdit(self.table)
            activity_edit.setPlaceholderText("Optional activity")
            self.table.setCellWidget(row_index, 8, activity_edit)
            # Dimer case: also import this structure as a general (screening) ligand, not a reference.
            as_ligand = QCheckBox(self.table)
            as_ligand.setToolTip("Also import this receptor as a general ligand (e.g. a homodimer).")
            self.table.setCellWidget(row_index, 9, as_ligand)

    def _apply_mode_visibility(self) -> None:
        mode = "receptor"
        hide_selection = mode == "receptor"
        self.table.setColumnHidden(7, hide_selection)
        self.table.setColumnHidden(8, hide_selection)
        for row_index in range(self.table.rowCount()):
            ligand_widget = self._ligand_widget(row_index)
            activity_widget = self._activity_widget(row_index)
            if ligand_widget is not None:
                ligand_widget.setEnabled(mode in {"redocking", "rescoring"})
                if mode in {"redocking", "rescoring"} and ligand_widget.currentIndex() <= 0 and ligand_widget.count() > 1:
                    ligand_widget.blockSignals(True)
                    ligand_widget.setCurrentIndex(1)
                    ligand_widget.blockSignals(False)
            if activity_widget is not None:
                activity_widget.setEnabled(mode == "rescoring")

    def start_scan(self) -> None:
        if self._scan_thread is not None:
            return
        self.status_label.setText(f"Processing 0/{len(self._file_paths)} receptor(s)...")
        self.progress_bar.setVisible(True)
        self._scan_thread = QThread(self)
        self._scan_worker = _ReceptorScanWorker(file_paths=self._file_paths)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.start()

    @Slot(int, int, str, dict)
    def _on_scan_progress(self, index: int, total: int, path: str, scan: dict) -> None:
        self._scan_cache_by_path[path] = scan
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(index)
        row_index = self._file_paths.index(path)
        preview = build_receptor_import_preview(scan, self._base_options())
        self._previews_by_path[path] = preview
        # Default this row's unit to the first biological assembly (same default the final
        # batch pass applies), so building its widgets now matches the end state.
        assemblies = [str(name) for name in list(scan.get("assemblies") or [])]
        self._row_state_by_path.setdefault(path, {}).setdefault(
            "selected_assembly", assemblies[0] if assemblies else ""
        )
        # Build this row's widgets (Unit/Chains/Ligands) as its scan lands, so receptors fill
        # in one by one instead of all appearing at once when the whole scan finishes.
        self._update_row_display(
            row_index=row_index,
            path=path,
            preview=preview,
            mode="receptor",
            review_count=0,
            candidate_count=0,
            build_widgets=True,
        )
        self.status_label.setText(f"{index}/{total} receptor(s) processed...")

    @Slot()
    def _on_scan_finished(self) -> None:
        self._scan_complete = True
        self.progress_bar.setVisible(False)
        self._scan_worker = None
        self._scan_thread = None
        self.refresh_preview()
        self._emit_ready(True)
        self._sync_file_controls()

    def _set_item(self, row: int, column: int, value: str) -> None:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, column, item)

    def _update_row_display(
        self,
        *,
        row_index: int,
        path: str,
        preview: dict,
        mode: str,
        review_count: int,
        candidate_count: int,
        build_widgets: bool,
    ) -> tuple[int, int]:
        summary = dict(preview.get("summary") or {})
        if str(summary.get("status") or "") != "OK":
            review_count += 1
        if list((preview.get("workflow") or {}).get("ligand_candidates") or []):
            candidate_count += 1
        self._set_item(row_index, 0, Path(path).name)
        # Columns 1 (Unit), 2 (Chains), 3 (Ligands) are widgets, populated below when build_widgets.
        self._set_item(row_index, 4, ", ".join(list(summary.get("metal_labels") or [])) or "0")
        self._set_item(row_index, 5, str(summary.get("waters") or 0))
        self._set_item(row_index, 6, str(summary.get("status") or "Review"))
        if build_widgets:
            scan = self._scan_cache_by_path.get(path) or {}
            assemblies = [str(name) for name in list(scan.get("assemblies") or [])]
            assembly_chains = {str(name): [str(label) for label in labels] for name, labels in dict(scan.get("assembly_chains") or {}).items()}
            all_chains = [str(chain.get("label")) for chain in list(scan.get("chains") or [])]

            # Unit combo: asymmetric unit + each biological assembly. Follow the captured state
            # (which defaults to the first biological assembly on the first pass).
            unit_combo = self.table.cellWidget(row_index, 1)
            chosen_assembly = ""
            if isinstance(unit_combo, QComboBox):
                target = str((self._row_state_by_path.get(path, {})).get("selected_assembly") or "")
                unit_combo.blockSignals(True)
                unit_combo.clear()
                unit_combo.addItem("Asymmetric unit", "")
                for name in assemblies:
                    unit_combo.addItem(f"Biological assembly {name}", name)
                index = unit_combo.findData(target)
                unit_combo.setCurrentIndex(index if index >= 0 else 0)
                unit_combo.blockSignals(False)
                chosen_assembly = str(unit_combo.currentData() or "")

            # Chains available depend on the chosen unit; keep prior selection where it still applies.
            if chosen_assembly and chosen_assembly in assembly_chains:
                unit_chains = [chain for chain in all_chains if chain in set(assembly_chains[chosen_assembly])]
            else:
                unit_chains = all_chains
            state = self._row_state_by_path.get(path, {})
            chain_button = self.table.cellWidget(row_index, 2)
            if isinstance(chain_button, _ChipsSelect):
                prior = [chain for chain in (state.get("selected_chain_ids") or []) if chain in unit_chains]
                selected = prior or None  # None → all chains of the unit
                chain_button.blockSignals(True)
                chain_button.set_items(unit_chains, selected=selected)
                chain_button.blockSignals(False)

            # Ligand chips: the cocrystal candidates (already chain-filtered) to keep as references.
            ligand_chips = self.table.cellWidget(row_index, 3)
            if isinstance(ligand_chips, _ChipsSelect):
                candidates = list((preview.get("workflow") or {}).get("ligand_candidates") or [])
                captured = state.get("selected_reference_ligands")
                selected = None if captured is None else [s for s in captured if s in candidates]
                ligand_chips.blockSignals(True)
                ligand_chips.set_items(candidates, selected=selected)
                ligand_chips.blockSignals(False)

            ligand_combo = self._ligand_widget(row_index)
            if ligand_combo is not None:
                current_value = str(ligand_combo.currentData() or "")
                new_values = list((preview.get("workflow") or {}).get("ligand_candidates") or [])
                selected_key = str((preview.get("workflow") or {}).get("selected_cocrystal_key") or current_value)
                ligand_combo.blockSignals(True)
                ligand_combo.clear()
                ligand_combo.addItem("None", "")
                for selector in new_values:
                    ligand_combo.addItem(selector, selector)
                selected_index = ligand_combo.findData(selected_key)
                ligand_combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
                ligand_combo.setEnabled(mode in {"redocking", "rescoring"})
                ligand_combo.blockSignals(False)

            activity_edit = self._activity_widget(row_index)
            if activity_edit is not None:
                current_text = str(activity_edit.text() or "").strip()
                next_text = str((preview.get("workflow") or {}).get("activity") or current_text)
                activity_edit.blockSignals(True)
                activity_edit.setText(next_text)
                activity_edit.setEnabled(mode == "rescoring")
                activity_edit.blockSignals(False)
        return review_count, candidate_count


def _receptor_table_config(runtime) -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("name", label="Name", width=220, sortable=True, filterable=True),
            ColumnDef("usage_class", label="Usage", width=110, sortable=True, filterable=True, visible=False,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeUsageClass)),
            ColumnDef("source", label="Source", width=360, sortable=True),
            ColumnDef("input_format", label="Format", width=90, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(FileFormat)),
            ColumnDef("current_path", label="Current Path", width=300, sortable=True, visible=False),
            ColumnDef("stored_path", label="Stored Path", width=300, sortable=True, visible=False),
        ],
        default_filters=[
            FilterSpec("is_receptor", FilterOperator.EQ, True, label="is_receptor"),
            FilterSpec("usage_class", FilterOperator.EQ, "general", label="general_only"),
            FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only"),
        ],
        # default_sort=[SortSpec("id", descending=True)],
        page_size=20,
        page_size_options=[10, 20, 50, 100],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        embedded_controls=True,  # Columns/Export · stretch · Reload/Settings
        context_menu_actions={"Create Receptor Set…": lambda objs: _create_receptor_set(runtime, objs)},
        toolbar_left=[
            # ponytail: activeWindow() is the main window at click time (no modal open then).
            ToolbarAction(label="Import…",
                          on_click=lambda objs: import_receptors_from_file(QApplication.activeWindow())),
            # ToolbarAction(label="Create Set…",
            #               on_click=lambda objs: _create_receptor_set(runtime, objs)),
        ],
        empty_message="No receptors loaded in the active project",
        empty_action=ToolbarAction(
            label="Import Receptors…", icon=icon("file-plus.svg"),
            on_click=lambda objs: import_receptors_from_file(QApplication.activeWindow()),
        ),
    )


class ReceptorWidget(BoundTableWidget):
    delete_kind = "molecule"

    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_receptor_table_config(runtime),
            empty_text="Open or create a project to inspect receptors.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._load_receptor_in_pymol)

    def _load_object_in_pymol(self, obj) -> None:
        self._load_receptor_in_pymol(obj)

    def _load_receptor_in_pymol(self, receptor: MoleculeRecord) -> None:
        receptor_path = current_molecule_path(receptor) or Path()
        main_window = self.window()
        detail_handler = getattr(main_window, "show_catalog_selection_details", None)
        if callable(detail_handler):
            detail_handler("receptor", receptor)
        if not receptor_path.exists():
            return
        dock = getattr(main_window, "pymol_dock", None)
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        object_name = f"receptor_{getattr(receptor, 'id', 'selected')}"
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(receptor_path), object_name)
            apply_receptor_atom_coloring(cmd, object_name)
            cmd.zoom(object_name, 3)
            cmd.orient(object_name)
            set_pymol_scene_context(
                dock,
                "receptor",
                target=object_name,
                selections={"receptor": object_name},
                default_preset="amdockvs.receptor",
            )
        except Exception:
            return


def import_receptors_from_file(window) -> None:
    active_context = getattr(window.runtime, "active_context", None)
    if active_context is None:
        QMessageBox.warning(
            window,
            "Import Receptors",
            "Open or create a project before importing receptors.",
        )
        return
    from amdockvs.ui.tools.import_workspace import open_import_view

    open_import_view(window, kind="receptor")


def _create_receptor_set(runtime, objects) -> None:
    """Snapshot set from the rows selected in the receptors table (Acciones ▾ / right-click)."""
    ids = [int(getattr(o, "id", 0) or 0) for o in objects if int(getattr(o, "id", 0) or 0) > 0]
    if not ids:
        QMessageBox.information(None, "Create Receptor Set", "Select at least one receptor.")
        return
    default_name = f"receptor_set_{datetime.now():%Y%m%d_%H%M%S}"
    set_name, accepted = QInputDialog.getText(None, "Create Receptor Set", "Set name", text=default_name)
    if not accepted:
        return
    try:
        set_ref = runtime.molecules.create_set(
            ids,
            name=str(set_name or "").strip() or default_name,
            kind="snapshot",
            metadata={"source": "ui.receptors.selection", "count": len(ids)},
        )
    except Exception as exc:
        QMessageBox.critical(None, "Create Receptor Set", str(exc))
        return
    QMessageBox.information(None, "Create Receptor Set", f"Created set #{set_ref.id} with {len(ids)} receptor(s).")


def register_receptors_workspace(window) -> None:
    window.register_main_view(
        RECEPTOR_VIEW_ID,
        "Receptors",
        lambda: ReceptorWidget(
            runtime=window.runtime,
            parent=window.central_widget,
        ),
    )

