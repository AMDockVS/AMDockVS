"""Receptor import: preview a structure's chains and ligands, choose what to keep, submit the job."""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QFrame,
)
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from ms_components.tool_panel import PROGRESS_NAME

from amdockvs.core.configuration import app_config
from amdockvs.io.downloads import discard_temporary
from amdockvs.io.formats import QT_FILE_FILTER
from amdockvs.io.receptor_preview import (
    COORDINATION_METAL_CODES,
    ReceptorImportOptions,
    build_receptor_import_preview,
    scan_receptor_structure,
)
from amdockvs.io.parsers.readers import parse_fasta
from amdockvs.ui.common.drop_area import (
    USER_INPUT_SOURCE,
    TablePlaceholder,
    drop_hint,
    expand_folders,
    side_toolbar,
    size_import_dialog,
    tool_action,
    write_user_input,
)
from amdockvs.ui.view_ids import RECEPTOR_VIEW_ID
from amdockvs.models.molecules import MoleculeType
from amdockvs.ui.common.widgets import WrapCombo
from ms_components.chips import ChipsSelect
from amdockvs.ui.tools.importing.common import land_on_view, nudge_monitor, submit_import
from amdockvs.ui.tools.importing.download import download_into, file_label, file_tip


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
        # One spawned process, not this thread: the scan is Python and would hold the GIL the GUI
        # needs to build rows. ponytail: one process = one core; ms_flow's jobs keep the rest.
        total = len(self._file_paths)
        try:
            with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
                for index, (path, scan) in enumerate(zip(self._file_paths, pool.map(scan_receptor_structure, self._file_paths)), start=1):
                    self.progress.emit(index, total, path, scan)
        finally:
            self.finished.emit()


class ReceptorImportPanel(QWidget):
    """Non-modal receptor import UI: options + per-file preview table (scan on a worker thread).

    Hosted by the dedicated Import workspace tab. Emits ready_changed(bool) when the scan
    finishes so the host can gate its "Queue Import" button. File-set edits are only allowed
    when no scan is running (the Add/Remove/Clear buttons are disabled during a scan).
    """

    ready_changed = Signal(bool)
    preview_finished = Signal()

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
        self.file_dialog_directory = ""
        self.file_dialog_options = QFileDialog.Option(0)
        self._show_file_controls = show_file_controls
        # Drag-and-drop feeds the same add path as the button, when file editing is available.
        self.setAcceptDrops(self._show_file_controls)
        self._ready = False
        self._file_paths = [str(Path(path).expanduser().resolve()) for path in (file_paths or [])]
        self._source_labels: dict[str, str] = {}  # temp carrier path → real origin ("user input")
        self.downloaded: set[str] = set()  # the host deletes the temporary ones it does not import
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
        self._pending_scans: list[tuple] = []  # landed scans not yet shown (see _drain_scans)
        self._scan_finish_pending = False

        root = QVBoxLayout(self)
        # One grid: checkboxes in column 0, the Grid Box size beside the option it belongs to,
        # and the status line on the right.
        options = QGridLayout()

        # Docking mode (docking / redocking / rescoring) is chosen in the Docking Studio, not here.
        # At import, every cocrystal ligand is registered as a reference automatically.

        # Assembly choice is per-row now (the "Unit" column), so no global bioassembly toggle.
        self.remove_non_structural_waters = QCheckBox("Remove non-structural waters", self)
        self.remove_non_structural_waters.setChecked(True)
        options.addWidget(self.remove_non_structural_waters, 0, 0)

        self.create_binding_sites = QCheckBox("Grid Box from ligands and coordinated metals", self)
        self.create_binding_sites.setChecked(True)
        options.addWidget(self.create_binding_sites, 1, 0)

        box_size_label = QLabel("Grid Box (Å)", self)
        self.binding_site_box_size = QDoubleSpinBox(self)
        self.binding_site_box_size.setRange(8.0, 40.0)
        self.binding_site_box_size.setDecimals(1)
        self.binding_site_box_size.setSingleStep(1.0)
        self.binding_site_box_size.setValue(app_config(self._runtime).docking.box_size)
        options.addWidget(box_size_label, 0, 2)
        options.addWidget(self.binding_site_box_size, 0, 3, Qt.AlignLeft)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.VLine)
        options.addWidget(separator, 0, 1, 4, 1)

        # Which coordinated metals center a Grid Box: the config's list, more typed in (Mn + Enter).
        metals_label = QLabel("Metals", self)
        metals_label.setToolTip("Coordinated metals that become the center of a Grid Box.")
        self.metals_box = ChipsSelect(self, editable=True, reorderable=True, normalize=lambda text: text.strip().upper())
        self.metals_box.setMinimumWidth(300)  # chips wrap in rows, not one per line
        configured = list(app_config(self._runtime).imports.grid_box_metals)
        self.metals_box.set_items(
            configured + [code for code in COORDINATION_METAL_CODES if code not in configured], selected=configured
        )
        options.addWidget(metals_label, 1, 2, Qt.AlignTop)
        options.addWidget(self.metals_box, 1, 3, 3, 1)
        # The size and metals only mean something when boxes are created from the cocrystal components.
        for widget in (box_size_label, self.binding_site_box_size, metals_label, self.metals_box):
            self.create_binding_sites.toggled.connect(widget.setEnabled)

        self.remove_altloc = QCheckBox("Resolve AltLoc to A / highest occupancy", self)
        self.remove_altloc.setChecked(True)
        options.addWidget(self.remove_altloc, 2, 0)

        self.remove_cofactors = QCheckBox("Remove cofactors during preparation", self)
        options.addWidget(self.remove_cofactors, 3, 0)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        options.addWidget(self.status_label, 0, 4, 4, 1, Qt.AlignRight | Qt.AlignVCenter)
        options.setColumnStretch(4, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, max(1, len(self._file_paths)))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(self._enable_preview)
        # Thin themed line (same as the tool panels' run progress), not a full-height bar.
        self.progress_bar.setObjectName(PROGRESS_NAME)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(3)
        # Not laid out here: the host puts it in its footer, beside the Import/Cancel buttons.

        self.table = QTableWidget(0, 12, self)
        self.table.setHorizontalHeaderLabels(
            ["File", "Unit", "Chains", "Ligands", "Metals", "Waters", "Status", "Selected Ligand", "Activity", "As Ligand", "Format", "Path"]
        )
        header = self.table.horizontalHeader()
        # Every column is user-resizable (Interactive) with a sensible default width; Chains needs
        # room for its chips+combo so it isn't clipped.
        header.setStretchLastSection(False)
        default_column_widths = {0: 90, 1: 120, 2: 245, 3: 295, 4: 190, 5: 190, 6: 80, 7: 160, 8: 120, 9: 80}
        for column, width in default_column_widths.items():
            if column in [2, 3]:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)

        # Status (6) only repeats what the status line under the table already counts.
        self.table.setColumnHidden(6, True)
        header.sectionResized.connect(lambda *_: self._fit_rows())
        # Selected Ligand (7) / Activity (8) belong to redocking/rescoring, chosen in Docking Studio.
        self.table.setColumnHidden(7, True)
        self.table.setColumnHidden(8, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header.setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)

        # Vertical add/remove toolbar on the table's left (where the eye starts), with a drop hint.
        table_row = QHBoxLayout()
        if self._show_file_controls:
            self._placeholder = TablePlaceholder(self.table, drop_hint("receptor"))
            self._placeholder.clicked.connect(self._on_add_files)
            toolbar = side_toolbar(self)
            self.add_files_action = tool_action(toolbar, "file-plus.svg", "Add files", "Add receptor files", self._on_add_files)
            self.add_folder_action = tool_action(
                toolbar, "folder-plus.svg", "Add folder", "Add every receptor file in a folder (and its subfolders)", self._on_add_folder
            )
            self.add_text_action = tool_action(
                toolbar, "text-input.svg", "Add sequence", "Add a sequence (FASTA or a bare sequence)", self._on_add_text
            )
            self.download_action = tool_action(
                toolbar, "cloud.svg", "Download", "Download from RCSB PDB, AlphaFold DB, ESM Atlas or UniProt", self._on_download
            )
            toolbar.addSeparator()
            self.remove_files_action = tool_action(
                toolbar, "shredder.svg", "Remove", "Remove the selected files", self._on_remove_selected_files
            )
            table_row.addWidget(toolbar)
        table_row.addWidget(self.table, 1)
        root.addLayout(table_row, 1)
        root.addLayout(options)

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
        self.metals_box.changed.connect(self.refresh_preview)

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
        self._pending_scans.clear()
        self._scan_finish_pending = False
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
        self._prime_table()
        self._apply_mode_visibility()
        if self._enable_preview:
            self._sync_file_controls()
            QTimer.singleShot(0, self.start_scan)
        else:
            self.refresh_preview()
            self._emit_ready(True)
            self._sync_file_controls()

    @property
    def file_paths(self) -> list[str]:
        return list(self._file_paths)

    @property
    def preview_running(self) -> bool:
        return any(thread.isRunning() for thread in self.findChildren(QThread))

    @Slot()
    def _on_preview_thread_finished(self) -> None:
        if not self.preview_running:
            self.preview_finished.emit()

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
        for column in (1, 2, 3, 4, 5, 9):
            self.table.setColumnHidden(column, not self._enable_preview)
        for column in (10, 11):
            self.table.setColumnHidden(column, self._enable_preview)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive if self._enable_preview else QHeaderView.ResizeMode.Stretch
        )
        if self._enable_preview:
            self.table.setColumnWidth(0, 90)
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
        self.add_files_action.setEnabled(can_edit)
        self.add_folder_action.setEnabled(can_edit)
        self.add_text_action.setEnabled(can_edit)
        self.download_action.setEnabled(can_edit)
        self.remove_files_action.setEnabled(can_edit and self.table.rowCount() > 0)

    def _on_add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Receptor Files",
            self.file_dialog_directory,
            QT_FILE_FILTER,
            options=self.file_dialog_options,
        )
        self._add_paths(paths)

    def _on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add Receptor Folder")
        if folder:
            self._add_paths([folder])

    def _on_add_text(self) -> None:
        """Typed FASTA (or a bare sequence): one protein without 3D per record."""
        text, ok = QInputDialog.getMultiLineText(
            self, "Add sequence", "FASTA or a bare amino-acid sequence (':' separates chains):"
        )
        records = parse_fasta(text, stem="sequence") if ok else []
        if not records:
            return
        fasta = "".join(f">{name}\n{sequence}\n" for name, sequence in records)
        path = write_user_input(fasta, ".fasta")
        self._source_labels[path] = USER_INPUT_SOURCE
        self._add_paths([path])

    def _on_download(self) -> None:
        download_into(self, "receptor", self._add_downloaded, self._runtime)

    def _add_downloaded(self, files: list[tuple[str, str]]) -> None:
        self.downloaded.update(path for path, _label in files)
        if not self._can_edit_files():
            return
        self._source_labels.update(files)
        self._add_paths([path for path, _label in files])

    def _add_paths(self, paths) -> None:
        resolved = expand_folders(paths or [])
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
        if not self._enable_preview and len(self._file_paths) <= RECEPTOR_PREVIEW_MAX_FILES:
            self.set_files(self._file_paths)
            return
        self._apply_preview_visibility()
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
            "grid_box_metals": list(base.grid_box_metals or ()),
            "remove_cofactors": base.remove_cofactors,
            "remove_altloc": base.remove_altloc,
            "use_biological_assembly": base.use_biological_assembly,
            "import_mode": base.import_mode,
            "per_file": dict(self._row_state_by_path) if self._enable_preview else {},
            "scans": dict(self._scan_cache_by_path) if self._enable_preview else {},
            "build_specs": bool(self._enable_preview),
            "source_labels": {p: label for p, label in self._source_labels.items() if p in self._file_paths},
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
            grid_box_metals=tuple(self.metals_box.selected_items()),
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
            if isinstance(chain_widget, ChipsSelect):
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
            if last_sig is not None and last_sig == sig and isinstance(ligand_chips, ChipsSelect):
                selected_reference_ligands = ligand_chips.selected_items()
            # Metals follow the same rule: a unit/chain change offers a new set, all kept again.
            metal_chips = self.table.cellWidget(row_index, 4)
            selected_metals = None
            if last_sig is not None and last_sig == sig and isinstance(metal_chips, ChipsSelect):
                selected_metals = metal_chips.selected_items()
            water_chips = self.table.cellWidget(row_index, 5)
            selected_waters = None
            if last_sig is not None and last_sig == sig and isinstance(water_chips, ChipsSelect):
                selected_waters = water_chips.selected_items()

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
                "selected_metals": selected_metals,
                "selected_waters": selected_waters,
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
                        "selected_metals": _as_ref_tuple(state.get("selected_metals")),
                        "selected_waters": _as_ref_tuple(state.get("selected_waters")),
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
        self._fit_rows()

    def _fit_rows(self) -> None:
        # Deferred: cell widgets (wrapped Unit text, chips) get their final width on the next pass.
        QTimer.singleShot(0, self.table.resizeRowsToContents)

    def _prime_table(self) -> None:
        # Plain items only: the per-row widgets are built when that row's scan lands
        # (_ensure_row_widgets), so thousands of files don't stall before the first row shows.
        self.table.clearContents()
        self.table.setRowCount(len(self._file_paths))
        for row_index, path in enumerate(self._file_paths):
            self._set_item(row_index, 0, file_label(path, Path(path).name), tip=file_tip(path))
            self._set_item(row_index, 6, "...")
            if not self._enable_preview:
                self._set_item(row_index, 10, Path(path).suffix[1:].upper())
                self._set_item(row_index, 11, path, tip=file_tip(path))

    def _ensure_row_widgets(self, row_index: int) -> None:
        if self.table.cellWidget(row_index, 1) is not None:
            return
        # Unit (col 1): asymmetric unit vs a biological assembly.
        unit_combo = WrapCombo(self.table)
        unit_combo.currentIndexChanged.connect(self.refresh_preview)
        self.table.setCellWidget(row_index, 1, unit_combo)
        # Chains (col 2) is a per-row multi-select; Ligands (col 3) picks which cocrystal
        # ligands become references (drop artifacts); Metals (col 4) and Waters (col 5, the
        # structural ones) which stay in the receptor.
        for column in (2, 3, 4, 5):
            chips = ChipsSelect(self.table)
            chips.changed.connect(self.refresh_preview)
            chips.heightChanged.connect(lambda chips=chips: self._fit_row_of(chips))
            self.table.setCellWidget(row_index, column, chips)
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

    def _fit_row_of(self, widget: QWidget) -> None:
        # Only the row whose chips re-wrapped: a whole-table resize per chip change is O(rows²).
        row = self.table.indexAt(widget.pos()).row()
        if row >= 0:
            self.table.resizeRowToContents(row)

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
        if not self._enable_preview or not self._file_paths or self._scan_thread is not None:
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
        self._scan_thread.finished.connect(self._on_preview_thread_finished)
        self._scan_thread.start()

    @Slot(int, int, str, dict)
    def _on_scan_progress(self, index: int, total: int, path: str, scan: dict) -> None:
        # Queue, then drain on a timer: scans from parallel processes land faster than rows
        # build, and showing them back-to-back would hold off every repaint until the last one.
        self._pending_scans.append((index, total, path, scan))
        if len(self._pending_scans) == 1:
            QTimer.singleShot(0, self._drain_scans)

    def _drain_scans(self) -> None:
        deadline = time.monotonic() + 0.03  # ~2 frames of work, then let Qt paint
        while self._pending_scans and time.monotonic() < deadline:
            self._show_scan(*self._pending_scans.pop(0))
        if self._pending_scans:
            QTimer.singleShot(0, self._drain_scans)
        elif self._scan_finish_pending:
            self._finish_scan()

    def _show_scan(self, index: int, total: int, path: str, scan: dict) -> None:
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
        if self._pending_scans:
            self._scan_finish_pending = True  # _drain_scans finishes once the queue is empty
        else:
            self._finish_scan()

    def _finish_scan(self) -> None:
        self._scan_finish_pending = False
        self._scan_complete = True
        self.progress_bar.setVisible(False)
        self._scan_worker = None
        self._scan_thread = None
        self.refresh_preview()
        self._emit_ready(True)
        self._sync_file_controls()

    def _set_item(self, row: int, column: int, value: str, tip: str = "") -> None:
        item = QTableWidgetItem(value)
        if tip:
            item.setToolTip(tip)
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
        self._set_item(row_index, 0, file_label(path, Path(path).name), tip=file_tip(path))
        # Columns 1 (Unit), 2 (Chains), 3 (Ligands) are widgets, populated below when build_widgets.
        self._set_item(row_index, 6, str(summary.get("status") or "Review"))
        if build_widgets:
            self._ensure_row_widgets(row_index)
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
            if isinstance(chain_button, ChipsSelect):
                prior = [chain for chain in (state.get("selected_chain_ids") or []) if chain in unit_chains]
                selected = prior or None  # None → all chains of the unit
                chain_button.blockSignals(True)
                chain_button.set_items(unit_chains, selected=selected)
                chain_button.blockSignals(False)

            # Ligand chips: the cocrystal candidates (already chain-filtered) to keep as references.
            ligand_chips = self.table.cellWidget(row_index, 3)
            if isinstance(ligand_chips, ChipsSelect):
                candidates = list((preview.get("workflow") or {}).get("ligand_candidates") or [])
                captured = state.get("selected_reference_ligands")
                selected = None if captured is None else [s for s in captured if s in candidates]
                ligand_chips.blockSignals(True)
                ligand_chips.set_items(candidates, selected=selected)
                ligand_chips.blockSignals(False)

            # Metal chips: the metals on the kept chains; one taken off is stripped from the receptor.
            metal_chips = self.table.cellWidget(row_index, 4)
            if isinstance(metal_chips, ChipsSelect):
                captured = state.get("selected_metals")
                metal_chips.blockSignals(True)
                metal_chips.set_items(list((preview.get("workflow") or {}).get("metal_candidates") or []), selected=captured)
                metal_chips.blockSignals(False)
            water_chips = self.table.cellWidget(row_index, 5)
            if isinstance(water_chips, ChipsSelect):
                water_chips.blockSignals(True)
                water_chips.set_items(list((preview.get("workflow") or {}).get("water_candidates") or []),
                                      selected=state.get("selected_waters"))
                water_chips.blockSignals(False)

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


class ReceptorImportDialog(QDialog):
    """Default receptor importer: the reusable ReceptorImportPanel (unchanged) hosted in a dialog."""

    jobs_submitted = Signal(object)

    def __init__(self, *, runtime, parent=None, defer=False):
        super().__init__(parent)
        self.runtime = runtime
        self._defer = bool(defer)  # see LigandImportDialog: defer -> capture a deferred submit
        self.deferred_submit = None
        self.deferred_name = ""
        self.setWindowTitle("Import Receptors")
        size_import_dialog(self)
        root = QVBoxLayout(self)
        self.panel = ReceptorImportPanel(runtime=runtime, show_file_controls=True, parent=self)
        root.addWidget(self.panel, 1)

        buttons = QDialogButtonBox(self)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.setSpacing(24)
        footer.addWidget(self.panel.progress_bar, 1)
        footer.addWidget(buttons)
        root.addLayout(footer)

        self.panel.ready_changed.connect(self.import_button.setEnabled)
        self.import_button.setEnabled(self.panel.is_ready())

    def done(self, result: int) -> None:
        # Temporary downloads that will not be imported: on cancel all, on accept the removed ones.
        imported = set(self.panel.file_paths) if result == QDialog.Accepted else set()
        discarded = self.panel.downloaded - imported
        if self.panel.preview_running:
            self.panel.preview_finished.connect(lambda: discard_temporary(discarded),
                                                Qt.ConnectionType.SingleShotConnection)
        else:
            discard_temporary(discarded)
        super().done(result)

    def workflow_submit(self, *, force: bool = False):
        """(submit, name) capturing the receptor import (+ any dimer ligand-role files) as a deferred
        job, or None if empty. Runs when the workflow runs."""
        files = list(self.panel.file_paths)
        if not files:
            QMessageBox.information(self, "Import Receptors", "Add at least one receptor file.")
            return None
        import_request = self.panel.import_request()
        ligand_role_files = self.panel.ligand_role_files()

        def submit(rt):
            job_ids: list = []
            # A temporary download both jobs read is the ligand job's to delete.
            shared = set(ligand_role_files)
            res = rt.loader.load_receptors(
                files, **import_request, temporary_files=[path for path in files if path not in shared], force=force)
            job_ids.extend(res if isinstance(res, (list, tuple)) else [res])
            # Dimer case: flagged receptors also enter the general screening set as biopolymer ligands.
            if ligand_role_files:
                res2 = rt.loader.load_ligands(
                    ligand_role_files, molecule_kind=MoleculeType.PROTEIN, primary_context="general",
                    source_labels=import_request.get("source_labels"), force=force,
                )
                job_ids.extend(res2 if isinstance(res2, (list, tuple)) else [res2])
            return [j for j in job_ids if j]

        return submit, f"Import {len(files)} receptor file(s)"

    def _on_accept(self) -> None:
        if self._defer:
            payload = self.workflow_submit()
            if payload is None:
                return
            self.deferred_submit, self.deferred_name = payload
            self.accept()
        else:
            self._do_import()

    def _do_import(self) -> None:
        def submitted(job_ids) -> None:
            self.jobs_submitted.emit(job_ids)
            land_on_view(self.parent(), RECEPTOR_VIEW_ID, job_ids)
            nudge_monitor(self.parent())
            self.accept()

        submit_import(self, "Import Receptors", self.runtime, list(self.panel.file_paths), submitted)

__all__ = ["ReceptorImportDialog", "ReceptorImportPanel"]
