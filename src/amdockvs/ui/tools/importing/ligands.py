"""Ligand import: a table of dropped files, each with its molecule type, and the import policy tabs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QVBoxLayout,
)
from ms_components.theme import color

from amdockvs.io.downloads import discard_temporary
from amdockvs.io.jobs import SHARD_SUGGEST_RECORDS
from amdockvs.io.parsers.readers import count_import_records
from amdockvs.models.molecules import MoleculeType
from amdockvs.core.vocab import ProjectMode
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
from amdockvs.io.formats import QT_FILE_FILTER as _LIGAND_FILTER
from amdockvs.ui.view_ids import LIGANDS_VIEW_ID, SHARDS_VIEW_ID
from amdockvs.ui.tools.importing.common import land_on_view, nudge_monitor, submit_import
from amdockvs.ui.tools.importing.download import download_into, file_label, file_tip
from amdockvs.ui.tools.importing.ligand_forms import (
    ImportActivityForm,
    ImportFilterCriteriaForm,
    ImportPrepareForm,
    finalize_import_prefilter_policy,
)


# Per-row molecule type choices. Default (first) is small molecule — the common ligand case.
_TYPE_CHOICES = (
    ("Small molecule", MoleculeType.SMALL_MOLECULE),
    ("Protein", MoleculeType.PROTEIN),
    ("Peptide", MoleculeType.PEPTIDE),
    ("Nucleotide", MoleculeType.NUCLEOTIDE),
    ("Polymer", MoleculeType.POLYMER),
)


_TYPE_LABELS = {value: label for label, value in _TYPE_CHOICES}
_REFERENCE_TIP = (
    "Curated set (cocrystals, measured actives): imported as rows, never shards, and the "
    "only files the Activity tab reads measurements from."
)


class _FileRowsModel(QAbstractTableModel):
    """The dropped files as plain rows [path, kind, reference]; the view paints only what it shows."""

    COLUMNS = ("Name", "Type", "Format", "Path", "As Reference")
    TYPE, REFERENCE = 1, 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[list] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        path, kind, reference = self.entries[index.row()]
        column = index.column()
        if role == Qt.CheckStateRole and column == self.REFERENCE:
            return Qt.Checked if reference else Qt.Unchecked
        if role == Qt.EditRole and column == self.TYPE:
            return kind
        if role == Qt.DisplayRole:
            name = file_label(path, Path(path).stem)
            return (name, f"{_TYPE_LABELS[kind]} ▾", (Path(path).suffix[1:] or "?").upper(), path, None)[column]
        if role == Qt.ToolTipRole:
            return _REFERENCE_TIP if column == self.REFERENCE else file_tip(path)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == self.TYPE:
            return flags | Qt.ItemIsEditable
        if index.column() == self.REFERENCE:
            return flags | Qt.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        entry = self.entries[index.row()]
        if index.column() == self.TYPE and role == Qt.EditRole and value in _TYPE_LABELS:
            entry[1] = value
        elif index.column() == self.REFERENCE and role == Qt.CheckStateRole:
            entry[2] = Qt.CheckState(value) == Qt.Checked
        else:
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def append(self, paths: list[str]) -> None:
        start = len(self.entries)
        self.beginInsertRows(QModelIndex(), start, start + len(paths) - 1)
        self.entries.extend([path, MoleculeType.SMALL_MOLECULE, False] for path in paths)
        self.endInsertRows()

    def remove(self, rows: set[int]) -> None:
        self.beginResetModel()
        self.entries = [entry for row, entry in enumerate(self.entries) if row not in rows]
        self.endResetModel()


class _TypeDelegate(QStyledItemDelegate):
    """The Type combo exists only while a cell is being edited, never one per row."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for label, value in _TYPE_CHOICES:
            combo.addItem(label, value)
        def commit(_index: int) -> None:
            self.commitData.emit(combo)
            self.closeEditor.emit(combo)

        combo.activated.connect(commit)
        return combo

    def setEditorData(self, editor, index) -> None:
        editor.setCurrentIndex(editor.findData(index.data(Qt.EditRole)))
        editor.showPopup()

    def setModelData(self, editor, model, index) -> None:
        model.setData(index, editor.currentData(), Qt.EditRole)


class _ReferenceDelegate(QStyledItemDelegate):
    """A click anywhere in the As Reference cell toggles it, not only on the small indicator."""

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            return True  # the release toggles; swallowing these keeps one click = one toggle
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            checked = index.data(Qt.CheckStateRole) == Qt.Checked
            return model.setData(index, Qt.Unchecked if checked else Qt.Checked, Qt.CheckStateRole)
        return super().editorEvent(event, model, option, index)


class FileDropTable(QTableView):
    """Drag-and-drop file list with Name / Type / Format / Path / As Reference columns, a per-row
    type (edited through a combo) and a per-row reference mark."""

    rows_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(_FileRowsModel(self))
        self.setItemDelegateForColumn(_FileRowsModel.TYPE, _TypeDelegate(self))
        self.setItemDelegateForColumn(_FileRowsModel.REFERENCE, _ReferenceDelegate(self))
        self.setEditTriggers(QAbstractItemView.EditKeyPressed)
        self.clicked.connect(lambda index: index.column() == _FileRowsModel.TYPE and self.edit(index))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        # ponytail: content-sized columns measure every row on each insert; Interactive keeps the
        # widths fixed (the user can still drag them), which is what lets 10k files load at once.
        for column, mode in enumerate((QHeaderView.Stretch, QHeaderView.Interactive, QHeaderView.Interactive,
                                       QHeaderView.Stretch, QHeaderView.Interactive)):
            header.setSectionResizeMode(column, mode)
        self.setColumnWidth(_FileRowsModel.TYPE, 130)
        self.setColumnWidth(2, 70)
        self.setColumnWidth(_FileRowsModel.REFERENCE, 100)
        self.setAcceptDrops(True)
        self.placeholder = TablePlaceholder(self, drop_hint("ligand"))
        self.model().dataChanged.connect(self.rows_changed)

    # ---- rows ----
    def add_files(self, paths) -> None:
        existing = {entry[0] for entry in self.model().entries}
        new: list[str] = []
        for resolved in expand_folders(paths):
            if resolved not in existing:
                existing.add(resolved)
                new.append(resolved)
        if new:
            self.model().append(new)
            self.rows_changed.emit()

    def remove_selected(self) -> None:
        rows = {index.row() for index in self.selectionModel().selectedRows()}
        if rows:
            self.model().remove(rows)
            self.rows_changed.emit()

    def rows(self) -> list[tuple[str, str]]:
        """[(path, molecule_kind)] for each row."""
        return [(path, kind) for path, kind, _reference in self.model().entries]

    def reference_paths(self) -> set[str]:
        """Paths ticked 'As Reference'."""
        return {path for path, _kind, reference in self.model().entries if reference}

    def has_small_molecule(self) -> bool:
        return any(kind == MoleculeType.SMALL_MOLECULE for _, kind in self.rows())

    # ---- drag & drop ----
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()


class LigandImportDialog(QDialog):
    """Default ligand importer: a familiar dialog with a drag-drop file table (left), an icon
    toolbar (right), and per-scope option tabs. Filter/QSAR/Activity tabs are disabled until at
    least one row is a small molecule (they don't apply otherwise). On import, files are grouped
    by molecule type: the small-molecule group carries the streaming prefilter, the rest just carry
    their kind.
    """

    jobs_submitted = Signal(object)

    def __init__(self, *, runtime, parent=None, defer=False):
        super().__init__(parent)
        self.runtime = runtime
        self.file_dialog_directory = ""
        self.file_dialog_options = QFileDialog.Option(0)
        # defer=True: used as a workflow step config — capture the import as a deferred submit
        # (run when the workflow runs) instead of importing now.
        self._defer = bool(defer)
        self.deferred_submit = None
        self.deferred_name = ""
        self._target_sharded = False
        self._project_sharded = False
        self.setWindowTitle("Import Ligands")
        size_import_dialog(self)
        root = QVBoxLayout(self)

        # --- top: vertical add/remove toolbar + file table ---
        top = QHBoxLayout()
        self.table = FileDropTable(self)
        top.addWidget(self.table, 1)

        self._source_labels: dict[str, str] = {}  # temp carrier path → real origin ("user input")
        self._downloaded: set[str] = set()  # temporary ones not imported are deleted in done()
        toolbar = side_toolbar(self)
        self.add_action = tool_action(toolbar, "file-plus.svg", "Add files", "Add ligand files", self._on_add)
        self.add_folder_action = tool_action(
            toolbar, "folder-plus.svg", "Add folder", "Add every ligand file in a folder (and its subfolders)", self._on_add_folder
        )
        self.add_text_action = tool_action(
            toolbar, "text-input.svg", "Add SMILES", "Add SMILES (one per line, optional name)", self._on_add_text
        )
        self.download_action = tool_action(
            toolbar, "cloud.svg", "Download", "Download from PubChem, ChEMBL, eMolecules, ZINC22 or a vendor link", self._on_download
        )
        toolbar.addSeparator()
        self.remove_action = tool_action(toolbar, "shredder.svg", "Remove", "Remove the selected files", self.table.remove_selected)
        self.table.placeholder.clicked.connect(self._on_add)
        top.insertWidget(0, toolbar)
        root.addLayout(top, 1)

        # --- where the library lands ---
        # This is the whole "campaign mode" decision, and it lives here because it is a property
        # of the library being imported, not of the project. One screening library per project:
        # once the project holds one, the box is locked to match it.
        # Framed and always explained: it is the most consequential choice in the dialog, and
        # the frame turns to the warning accent while it is ticked.
        self._record_counts: dict[str, int] = {}
        self.shard_box = QFrame(self)
        self.shard_box.setObjectName("shardBox")
        box_layout = QVBoxLayout(self.shard_box)
        self.shard_checkbox = QCheckBox("Screening library: keep on disk as shards", self.shard_box)
        bold = self.shard_checkbox.font()
        bold.setBold(True)
        self.shard_checkbox.setFont(bold)
        self.shard_checkbox.setToolTip(
            "For libraries too big to materialize. Receptors and reference ligands are unaffected."
        )
        description = QLabel(
            "Molecules stay in files on disk: no rows, no catalog, and only docking hits are ever "
            "written to the project. This sets how the whole project screens - one screening "
            "library per project.",
            self.shard_box,
        )
        description.setWordWrap(True)
        self.shard_hint = QLabel("", self.shard_box)
        self.shard_hint.setWordWrap(True)
        self.shard_hint.setVisible(False)
        for widget in (self.shard_checkbox, description, self.shard_hint):
            box_layout.addWidget(widget)
        root.addWidget(self.shard_box)
        self.shard_checkbox.toggled.connect(self._style_shard_box)
        self._lock_shard_choice_to_project()
        self._style_shard_box()

        # --- option tabs (separate scopes) ---
        self.tabs = QTabWidget(self)
        self.filters_form = ImportFilterCriteriaForm(self)
        self.prepare_form = ImportPrepareForm(self)
        self.activity_form = ImportActivityForm(self)
        self.tabs.addTab(self.filters_form, "Filters")
        self.tabs.addTab(self.prepare_form, "Prepare")
        self.tabs.addTab(
            self._placeholder_tab(
                "QSAR enrichment filters are coming soon. Train and validate QSAR models in the "
                "QSAR workspace first; this import-time filter will only be enabled once the model "
                "feature contract is explicit."
            ),
            "QSAR",
        )
        self.tabs.addTab(self.activity_form, "Activity")
        # ponytail: post-materialization steps — placeholder tabs until the at-import path lands.
        self.tabs.addTab(
            self._placeholder_tab("Clustering runs after import, over the materialized set. Use Molecule Tools › Diversity for now."),
            "Clustering",
        )
        self.tabs.addTab(
            self._placeholder_tab("Diversity selection runs after import, over the materialized set. Use Molecule Tools › Diversity for now."),
            "Diverse",
        )
        root.addWidget(self.tabs)
        self._reference_count = 0
        self._activity_source: str | None = None

        # --- footer ---
        buttons = QDialogButtonBox(self)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.table.rows_changed.connect(self._sync_tabs_enabled)
        self._sync_tabs_enabled()

    # ---- helpers ----
    def _placeholder_tab(self, message: str) -> QLabel:
        label = QLabel(message, self)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignTop)
        return label

    def _on_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add ligand files", self.file_dialog_directory, _LIGAND_FILTER,
            options=self.file_dialog_options,
        )
        if paths:
            self.table.add_files(paths)

    def _on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add ligand folder")
        if folder:
            self.table.add_files([folder])

    def _on_add_text(self) -> None:
        """Typed SMILES become a .smi file; the molecules' source says "user input"."""
        text, ok = QInputDialog.getMultiLineText(
            self, "Add SMILES", "One SMILES per line, optionally followed by a name:"
        )
        if not ok or not text.strip():
            return
        path = write_user_input(text, ".smi")
        self._source_labels[path] = USER_INPUT_SOURCE
        self.table.add_files([path])

    def _on_download(self) -> None:
        download_into(self, "ligand", self._add_downloaded, self.runtime)

    def _add_downloaded(self, files: list[tuple[str, str]]) -> None:
        self._downloaded.update(path for path, _label in files)
        self._source_labels.update(files)
        self.table.add_files([path for path, _label in files])

    def _lock_shard_choice_to_project(self) -> None:
        """A project that already has a screening library cannot get a second one."""
        try:
            sharded = self.runtime.mode == ProjectMode.HTPVS
            rows = self.runtime.general_ligand_count()
        except Exception:  # noqa: BLE001 — no active project yet (workflow step config)
            return
        self._project_sharded = sharded
        if sharded:
            # ponytail: checked, but not locked. A sharded project still takes curated ligands
            # (cocrystals to redock, an activity set to train on); those are rows, not a second
            # library, so unticking here means "reference", not "shard this too".
            self.shard_checkbox.setChecked(True)
            self._set_shard_hint(
                "This project's library is sharded - ticked, these files join the shards. "
                "Untick to bring them in as reference ligands (rows: cocrystals, activity sets)."
            )
        elif rows:
            self.shard_checkbox.setChecked(False)
            self.shard_checkbox.setEnabled(False)
            self._set_shard_hint(
                f"This project already holds {rows} ligands as rows - imports go to the database."
            )

    def _style_shard_box(self, *_args) -> None:
        # The accent is read at style time, so changeEvent re-runs this on a theme switch.
        checked = self.shard_checkbox.isChecked()
        accent = color("yellow").name()
        border = accent if checked else "palette(mid)"
        self.shard_box.setStyleSheet(
            f"QFrame#shardBox {{ border: 1px solid {border}; border-radius: 4px; }}"
        )
        self.shard_hint.setStyleSheet(f"color: {accent};" if checked else "color: palette(placeholder-text);")

    def _set_shard_hint(self, text: str) -> None:
        # Hidden while empty: a blank word-wrapped label still reserves a line inside the frame.
        self.shard_hint.setText(text)
        self.shard_hint.setVisible(bool(text))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange) and hasattr(self, "shard_box"):
            self._style_shard_box()

    def _suggest_shards(self) -> int:
        """Approximate molecules queued in the small-molecule files; suggests shards past the threshold."""
        total = 0
        references = self.table.reference_paths()  # curated sets never shard, so they don't count
        for path, kind in self.table.rows():
            if kind != MoleculeType.SMALL_MOLECULE or path in references:
                continue
            # ponytail: counted once per path (a sampled estimate above 32 MB, exact below);
            # a file edited while the dialog is open keeps its first count.
            if path not in self._record_counts:
                try:
                    self._record_counts[path] = count_import_records(path, approx=True)
                except OSError:
                    self._record_counts[path] = 0
            total += self._record_counts[path]
        if self._project_sharded:  # the checkbox already says where this goes; keep that hint
            return total
        if self.shard_checkbox.isEnabled() and total >= SHARD_SUGGEST_RECORDS:
            self.shard_checkbox.setChecked(True)
            self._set_shard_hint(
                f"⚠ ~{total:,} molecules queued - sharding suggested. Importing them as rows "
                "would write a row per molecule into the project database."
            )
        elif self.shard_checkbox.isEnabled():
            self._set_shard_hint("")
        return total

    def _sync_activity_tab(self) -> None:
        """Measurements only come with a file marked 'As Reference'.

        Nothing is inferred from the file: a numeric column is not an assay. The mark is the
        declaration, and making it brings the Activity tab forward. A reference file is always
        rows, so activities can never be dropped by a shard import.
        """
        references = self.table.reference_paths()
        index = self.tabs.indexOf(self.activity_form)
        self.tabs.setTabEnabled(index, bool(references))
        self.tabs.setTabToolTip(
            index, "" if references else "Tick 'As Reference' on the files that carry activities."
        )
        if len(references) > self._reference_count:
            self.tabs.setCurrentWidget(self.activity_form)
        self._reference_count = len(references)
        # The first tabular reference file feeds the column picker; re-sniffed only when it
        # changes, so ticking another row does not reset the chips the user already picked.
        tabular = next(
            (path for path, _kind in self.table.rows()
             if path in references and Path(path).suffix.lower() in {".csv", ".tsv", ".txt", ".smi", ".smiles"}),
            None,
        )
        if tabular != self._activity_source:
            self._activity_source = tabular
            self.activity_form.set_source_file(tabular)

    def _sync_tabs_enabled(self) -> None:
        # Item 9: every option tab is off until a small-molecule candidate exists.
        self.tabs.setEnabled(self.table.has_small_molecule())
        self._sync_activity_tab()
        self._suggest_shards()

    def _policy_mapping(self, *, reference: bool = False):
        policy = {"target_molecule_kinds": ["small_molecule"]}
        self.filters_form.contribute(policy)
        self.prepare_form.contribute(policy)
        if reference:  # measurements belong to the files marked as reference, nobody else
            self.activity_form.contribute(policy)
        return finalize_import_prefilter_policy(policy)

    def workflow_submit(self, *, force: bool = False):
        """Validate the current selection and return (submit, name) where submit(runtime) performs
        the import (grouped by type, small-molecule group carrying the prefilter). None if empty.
        Captures the files + policy so the import runs later, when the workflow runs."""
        rows = self.table.rows()
        if not rows:
            QMessageBox.information(self, "Import Ligands", "Add at least one ligand file.")
            return None
        policy = self._policy_mapping()
        reference_policy = self._policy_mapping(reference=True)
        references = self.table.reference_paths()
        source_labels = {path: self._source_labels[path] for path, _kind in rows if path in self._source_labels}
        groups: dict[tuple[str, bool], list[str]] = {}
        for path, kind in rows:
            groups.setdefault((kind, path in references), []).append(path)
        total = len(rows)

        shard = self.shard_checkbox.isChecked()
        queued = self._suggest_shards() if not shard and not self._project_sharded else 0
        if queued >= SHARD_SUGGEST_RECORDS:
            answer = QMessageBox.question(
                self,
                "Import Ligands",
                f"Importing these files as rows will write ~{queued:,} rows into the project "
                "database.\n\nImport as shards instead?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return None
            shard = answer == QMessageBox.Yes

        # Where _do_import should land the user: Shards only if something actually goes there.
        self._target_sharded = shard and any(
            kind == MoleculeType.SMALL_MOLECULE and path not in references for path, kind in rows
        )

        def submit(rt):
            job_ids: list = []
            for (kind, reference), paths in groups.items():
                small = kind == MoleculeType.SMALL_MOLECULE
                prefilter = (reference_policy if reference else policy) if small else None
                # Only the screening library shards; reference files and other types are rows.
                if shard and small and not reference:
                    res = rt.loader.shard_ligands(paths, molecule_kind=kind, prefilter=prefilter, force=force)
                else:
                    res = rt.loader.load_ligands(
                        paths, molecule_kind=kind, prefilter=prefilter,
                        primary_context="reference" if reference else "general",
                        source_labels=source_labels, force=force,
                    )
                job_ids.extend(res if isinstance(res, (list, tuple)) else [res])
            return [j for j in job_ids if j]

        return submit, f"Import {total} ligand file(s)"

    def _on_accept(self) -> None:
        if self._defer:  # workflow config: capture the deferred submit, don't import now
            payload = self.workflow_submit()
            if payload is None:
                return
            self.deferred_submit, self.deferred_name = payload
            self.accept()
        else:
            self._do_import()

    def done(self, result: int) -> None:
        # Temporary downloads that will not be imported: on cancel all, on accept the removed ones.
        imported = {path for path, _kind in self.table.rows()} if result == QDialog.Accepted else set()
        discard_temporary(self._downloaded - imported)
        super().done(result)

    def _do_import(self) -> None:
        def submitted(job_ids) -> None:
            # Land where the molecules are actually going: staying on an empty Ligands table
            # while a shard import runs reads as "nothing happened".
            self.jobs_submitted.emit(job_ids)
            land_on_view(self.parent(), SHARDS_VIEW_ID if self._target_sharded else LIGANDS_VIEW_ID, job_ids)
            nudge_monitor(self.parent())
            self.accept()

        files = [path for path, _kind in self.table.rows()]
        submit_import(self, "Import Ligands", self.runtime, files, submitted)

__all__ = ["FileDropTable", "LigandImportDialog"]
