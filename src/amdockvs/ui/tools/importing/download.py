"""The importers' Download button: pick a database, type ids, get files into the table."""
from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QVBoxLayout,
)

from amdockvs.io.downloads import (
    Cancelled,
    Outcome,
    download,
    discard_temporary,
    is_temporary,
    parse_ids,
    sources_for,
    temporary_folder,
)
from amdockvs.ui.common.async_query import run_async

_NO_IDS = "Nothing to type: this source downloads one whole library."
TEMPORARY_TIP = "Temporary download: deleted once the import that reads it has completed."


def file_label(path: str, name: str) -> str:
    """The name an importer table shows for ``path``: temporary downloads say so."""
    return f"{name} (temporary)" if is_temporary(path) else name


def file_tip(path: str) -> str:
    return f"{path}\n{TEMPORARY_TIP}" if is_temporary(path) else path


class DownloadDialog(QDialog):
    def __init__(self, kind: str, parent=None, *, project_root=None):
        super().__init__(parent)
        self.setWindowTitle("Download")
        self.resize(560, 320)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.source_combo = QComboBox(self)
        for source in sources_for(kind):
            self.source_combo.addItem(source.label, source)
        form.addRow("Source", self.source_combo)
        root.addLayout(form)
        self.ids_edit = QPlainTextEdit(self)
        root.addWidget(self.ids_edit, 1)
        self.keep_checkbox = QCheckBox("Keep the downloaded files (reused on the next import)", self)
        self.keep_checkbox.setToolTip(
            "Kept files stay in the AMDockVS downloads folder. Otherwise they go to the project's "
            "tmp folder and are deleted once the import has completed. Settings > Downloads lists both."
        )
        # Without a project there is no tmp folder to put them in.
        self.keep_checkbox.setChecked(project_root is None)
        self.keep_checkbox.setEnabled(project_root is not None)
        root.addWidget(self.keep_checkbox)
        buttons = QDialogButtonBox(self)
        self.download_button = buttons.addButton("Download", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.source_combo.currentIndexChanged.connect(self._sync)
        self.ids_edit.textChanged.connect(self._sync)
        self._sync()

    @property
    def source(self):
        return self.source_combo.currentData()

    def ids(self) -> list[str]:
        return parse_ids(self.source, self.ids_edit.toPlainText())

    def _sync(self, *_args) -> None:
        takes_ids = bool(self.source.hint)
        self.ids_edit.setEnabled(takes_ids)
        self.ids_edit.setPlaceholderText(self.source.hint or _NO_IDS)
        self.download_button.setEnabled(not takes_ids or bool(self.ids()))


def download_into(parent, kind: str, add: Callable[[list[tuple[str, str]]], None], runtime=None) -> None:
    """Ask for a source and ids, download off the GUI thread, hand ``(path, label)`` pairs to ``add``."""
    try:
        project_root = runtime.get_project_paths()["project_root"]
    except Exception:  # noqa: BLE001 — no runtime or no open project: downloads are kept
        project_root = None
    dialog = DownloadDialog(kind, parent, project_root=project_root)
    if dialog.exec() != QDialog.Accepted:
        return
    source, ids = dialog.source, dialog.ids()
    folder = None if dialog.keep_checkbox.isChecked() else temporary_folder(project_root, source)

    cancel = threading.Event()
    state = {"done": 0, "total": 0, "what": source.label}
    progress = QProgressDialog(f"Downloading from {source.label}…", "Cancel", 0, 0, parent)
    progress.setWindowTitle("Download")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.canceled.connect(cancel.set)

    def refresh() -> None:
        total = state["total"]
        # Byte counts overflow QProgressDialog's int: show percent when the size is known.
        progress.setMaximum(100 if total else 0)
        progress.setValue(min(100, state["done"] * 100 // total) if total else 0)
        progress.setLabelText(f"Downloading from {source.label}…\n{state['what']}")

    timer = QTimer(progress)
    timer.timeout.connect(refresh)
    timer.start(150)
    progress.show()

    def report(done: int, total: int, what: str) -> None:  # worker thread: plain dict writes only
        state.update(done=done, total=total, what=what)

    def finish() -> None:
        timer.stop()
        progress.reset()
        progress.deleteLater()

    def on_result(outcome: Outcome) -> None:
        finish()
        if outcome.files:
            add(outcome.files)
        if outcome.failures:
            shown = "\n".join(outcome.failures[:20])
            more = f"\n… and {len(outcome.failures) - 20} more" if len(outcome.failures) > 20 else ""
            QMessageBox.warning(parent, "Download", f"Some entries could not be downloaded:\n\n{shown}{more}")

    def on_error(exc: Exception) -> None:
        finish()
        if folder is not None:  # what a cancelled or failed temporary download left behind
            discard_temporary([folder.parent])
        if not isinstance(exc, Cancelled):
            QMessageBox.critical(parent, "Download", f"Download from {source.label} failed:\n{exc}")

    run_async(lambda: download(source, ids, folder=folder, progress=report, cancel=cancel), on_result, on_error)
