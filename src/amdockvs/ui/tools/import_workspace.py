from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from amdockvs.io.jobs import SHARD_SUGGEST_RECORDS
from amdockvs.io.parsers.readers import count_import_records
from amdockvs.models.molecules import MoleculeType
from amdockvs.core.vocab import ProjectMode
from amdockvs.ui.common.drop_area import (
    USER_INPUT_SOURCE,
    TablePlaceholder,
    drop_hint,
    icon_button,
    toolbar_separator,
    write_user_input,
)
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.catalog.shards import SHARDS_VIEW_ID
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID, ReceptorImportPanel
from amdockvs.ui.tools.molecules.filter import (
    ImportActivityForm,
    ImportFilterCriteriaForm,
    ImportPrepareForm,
    finalize_import_prefilter_policy,
)

from amdockvs.io.formats import QT_FILE_FILTER as _LIGAND_FILTER
from ms_components.theme import color

# Per-row molecule type choices. Default (first) is small molecule — the common ligand case.
_TYPE_CHOICES = (
    ("Small molecule", MoleculeType.SMALL_MOLECULE),
    ("Protein", MoleculeType.PROTEIN),
    ("Peptide", MoleculeType.PEPTIDE),
    ("Nucleotide", MoleculeType.NUCLEOTIDE),
    ("Polymer", MoleculeType.POLYMER),
)


def _land_on_view(window, view_id: str, job_ids=(), *, noun: str = "Import") -> None:
    """Open the table the import fills and put the jobs' progress under it, where the eye is."""
    if window is None or not hasattr(window, "open_or_focus_view"):
        return
    view = window.open_or_focus_view(view_id)
    table = view if isinstance(view, BoundTableWidget) else view.findChild(BoundTableWidget)
    if table is not None:
        table.follow_jobs(job_ids, noun=noun, stage="Importing")


def _nudge_monitor(window) -> None:
    # The monitor backs off to a ~3s idle poll, so a freshly submitted job (and its progress
    # bar) can take that long to appear. Poke it to poll now so the bar shows immediately.
    bridge = getattr(window, "monitor_bridge", None)
    if bridge is not None and hasattr(bridge, "request_refresh"):
        bridge.request_refresh()


class FileDropTable(QTableWidget):
    """Drag-and-drop file list with Name / Type / Format / Path / As Reference columns, a per-row
    type combo and a per-row reference mark."""

    rows_changed = Signal()
    _COLUMNS = ("Name", "Type", "Format", "Path", "As Reference")

    def __init__(self, parent=None):
        super().__init__(0, len(self._COLUMNS), parent)
        self.setHorizontalHeaderLabels(list(self._COLUMNS))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.setAcceptDrops(True)
        self.placeholder = TablePlaceholder(self, drop_hint("ligand"))

    # ---- rows ----
    def _paths(self) -> set[str]:
        return {self.item(row, 3).text() for row in range(self.rowCount())}

    def add_files(self, paths) -> None:
        existing = self._paths()
        added = False
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            if resolved in existing:
                continue
            existing.add(resolved)
            self._append_row(resolved)
            added = True
        if added:
            self.rows_changed.emit()

    def _append_row(self, path: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        p = Path(path)
        name_item = QTableWidgetItem(p.stem)
        name_item.setToolTip(path)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        self.setItem(row, 0, name_item)

        type_combo = QComboBox(self)
        for label, value in _TYPE_CHOICES:
            type_combo.addItem(label, value)
        type_combo.currentIndexChanged.connect(self.rows_changed)
        self.setCellWidget(row, 1, type_combo)

        fmt_item = QTableWidgetItem((p.suffix[1:] or "?").upper())
        fmt_item.setFlags(fmt_item.flags() & ~Qt.ItemIsEditable)
        self.setItem(row, 2, fmt_item)

        path_item = QTableWidgetItem(path)
        path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
        self.setItem(row, 3, path_item)

        as_reference = QCheckBox(self)
        as_reference.setToolTip(
            "Curated set (cocrystals, measured actives): imported as rows, never shards, and the "
            "only files the Activity tab reads measurements from."
        )
        as_reference.toggled.connect(self.rows_changed)
        self.setCellWidget(row, 4, as_reference)

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        for row in rows:
            self.removeRow(row)
        if rows:
            self.rows_changed.emit()

    def rows(self) -> list[tuple[str, str]]:
        """[(path, molecule_kind)] for each row."""
        out: list[tuple[str, str]] = []
        for row in range(self.rowCount()):
            path = self.item(row, 3).text()
            kind = self.cellWidget(row, 1).currentData()
            out.append((path, kind))
        return out

    def reference_paths(self) -> set[str]:
        """Paths ticked 'As Reference'."""
        return {self.item(row, 3).text() for row in range(self.rowCount()) if self.cellWidget(row, 4).isChecked()}

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

    def __init__(self, *, runtime, parent=None, defer=False):
        super().__init__(parent)
        self.runtime = runtime
        # defer=True: used as a workflow step config — capture the import as a deferred submit
        # (run when the workflow runs) instead of importing now.
        self._defer = bool(defer)
        self.deferred_submit = None
        self.deferred_name = ""
        self._target_sharded = False
        self._project_sharded = False
        self.setWindowTitle("Import Ligands")
        self.resize(760, 580)
        root = QVBoxLayout(self)

        # --- top: file table + vertical icon toolbar ---
        top = QHBoxLayout()
        self.table = FileDropTable(self)
        top.addWidget(self.table, 1)

        toolbar = QVBoxLayout()
        self._source_labels: dict[str, str] = {}  # temp carrier path → real origin ("user input")
        self.add_button = icon_button(self, "file-plus.svg", "Add files")
        self.add_text_button = icon_button(self, "text-input.svg", "Add SMILES (one per line, optional name)")
        self.remove_button = icon_button(self, "shredder.svg", "Remove selected files")
        self.add_button.clicked.connect(self._on_add)
        self.add_text_button.clicked.connect(self._on_add_text)
        self.table.placeholder.clicked.connect(self._on_add)
        self.remove_button.clicked.connect(self.table.remove_selected)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.add_text_button)
        toolbar.addWidget(toolbar_separator(self))
        toolbar.addWidget(self.remove_button)
        toolbar.addStretch(1)
        top.addLayout(toolbar)
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
            "written to the project. This sets how the whole project screens — one screening "
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
        paths, _ = QFileDialog.getOpenFileNames(self, "Add ligand files", "", _LIGAND_FILTER)
        if paths:
            self.table.add_files(paths)

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
                "This project's library is sharded — ticked, these files join the shards. "
                "Untick to bring them in as reference ligands (rows: cocrystals, activity sets)."
            )
        elif rows:
            self.shard_checkbox.setChecked(False)
            self.shard_checkbox.setEnabled(False)
            self._set_shard_hint(
                f"This project already holds {rows} ligands as rows — imports go to the database."
            )

    def _style_shard_box(self, *_args) -> None:
        # The accent is read at style time, so changeEvent re-runs this on a theme switch.
        checked = self.shard_checkbox.isChecked()
        accent = color("yellow").name()
        border = accent if checked else "palette(mid)"
        self.shard_box.setStyleSheet(
            f"QFrame#shardBox {{ border: 1px solid {border}; border-radius: 4px; }}"
        )
        self.shard_hint.setStyleSheet(f"color: {accent};" if checked else "color: palette(mid);")

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
                f"⚠ ~{total:,} molecules queued — sharding suggested. Importing them as rows "
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

    def workflow_submit(self):
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
            # A row import into a sharded project is curated by definition — the library is on
            # disk, so these cannot be a second one. Read at run time: a workflow step may be
            # configured before the project holds any shards.
            context = "reference" if (not shard and rt.mode == ProjectMode.HTPVS) else "general"
            for (kind, reference), paths in groups.items():
                small = kind == MoleculeType.SMALL_MOLECULE
                prefilter = (reference_policy if reference else policy) if small else None
                # Only the screening library shards; reference files and other types are rows.
                if shard and small and not reference:
                    res = rt.loader.shard_ligands(paths, molecule_kind=kind, prefilter=prefilter)
                else:
                    res = rt.loader.load_ligands(
                        paths, molecule_kind=kind, prefilter=prefilter,
                        primary_context="reference" if reference else context,
                        source_labels=source_labels,
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

    def _do_import(self) -> None:
        payload = self.workflow_submit()
        if payload is None:
            return
        submit, _name = payload
        try:
            job_ids = submit(self.runtime)
        except Exception as exc:
            QMessageBox.critical(self, "Import Ligands", f"Could not submit ligand import job:\n{exc}")
            return
        # Land where the molecules are actually going: staying on an empty Ligands table
        # while a shard import runs reads as "nothing happened".
        _land_on_view(self.parent(), SHARDS_VIEW_ID if self._target_sharded else LIGANDS_VIEW_ID, job_ids)
        _nudge_monitor(self.parent())
        self.accept()


class ReceptorImportDialog(QDialog):
    """Default receptor importer: the reusable ReceptorImportPanel (unchanged) hosted in a dialog."""

    def __init__(self, *, runtime, parent=None, defer=False):
        super().__init__(parent)
        self.runtime = runtime
        self._defer = bool(defer)  # see LigandImportDialog: defer -> capture a deferred submit
        self.deferred_submit = None
        self.deferred_name = ""
        self.setWindowTitle("Import Receptors")
        self.resize(1140, 640)
        root = QVBoxLayout(self)
        self.panel = ReceptorImportPanel(runtime=runtime, show_file_controls=True, parent=self)
        root.addWidget(self.panel, 1)

        buttons = QDialogButtonBox(self)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.panel.ready_changed.connect(self.import_button.setEnabled)
        self.import_button.setEnabled(self.panel.is_ready())

    def workflow_submit(self):
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
            res = rt.loader.load_receptors(files, **import_request)
            job_ids.extend(res if isinstance(res, (list, tuple)) else [res])
            # Dimer case: flagged receptors also enter the general screening set as biopolymer ligands.
            if ligand_role_files:
                res2 = rt.loader.load_ligands(
                    ligand_role_files, molecule_kind=MoleculeType.PROTEIN, primary_context="general",
                    source_labels=import_request.get("source_labels"),
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
        payload = self.workflow_submit()
        if payload is None:
            return
        submit, _name = payload
        try:
            job_ids = submit(self.runtime)
        except Exception as exc:
            QMessageBox.critical(self, "Import Receptors", f"Could not submit receptor import job:\n{exc}")
            return
        _land_on_view(self.parent(), RECEPTOR_VIEW_ID, job_ids)
        _nudge_monitor(self.parent())
        self.accept()


def open_import_view(window, *, kind: str = "ligand"):
    """Both importers are now familiar dialogs."""
    if str(kind).lower().startswith("receptor"):
        dialog = ReceptorImportDialog(runtime=window.runtime, parent=window)
    else:
        dialog = LigandImportDialog(runtime=window.runtime, parent=window)
    dialog.exec()
    return dialog


__all__ = [
    "FileDropTable",
    "LigandImportDialog",
    "ReceptorImportDialog",
    "open_import_view",
]
