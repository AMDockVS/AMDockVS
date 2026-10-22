"""Settings ▸ Downloads: what the importers' Download button left on disk, and a way to delete it.

Kept files live in the AMDockVS downloads folder (shared by every project); temporary ones in the
open project's ``tmp/downloads`` until the import that reads them completes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.io.downloads import SOURCES, delete_files, kept_root, stored_files, temporary_root
from amdockvs.runtime import NON_TERMINAL_JOB_STATUSES
from amdockvs.ui.shell.settings_tools import _human_size

_KEPT, _TEMPORARY = "Kept", "Temporary"


class DownloadsPage(QWidget):
    def __init__(self, runtime, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        intro = QLabel(
            "Files fetched by the importers' Download button. Kept files are reused by every "
            "project; temporary ones belong to the open project and go once their import completes.",
            self,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["File", "Source", "Kind", "Size"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        root.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.summary = QLabel(self)
        actions.addWidget(self.summary, 1)
        for text, slot in (("Refresh", self.refresh), ("Delete selected", self.delete_selected),
                           ("Delete all", self.delete_all)):
            button = QPushButton(text, self)
            button.clicked.connect(slot)
            actions.addWidget(button)
        root.addLayout(actions)
        self.refresh()

    def showEvent(self, event) -> None:  # the settings dialog is reused: list what is there now
        super().showEvent(event)
        self.refresh()

    def _roots(self) -> list[tuple[str, object]]:
        roots = [(_KEPT, kept_root())]
        try:
            roots.append((_TEMPORARY, temporary_root(self.runtime.get_project_paths()["project_root"])))
        except Exception:  # noqa: BLE001 — no open project: no temporary downloads to show
            pass
        return roots

    def refresh(self) -> None:
        # ponytail: listed on the GUI thread; move to run_async if a downloads folder grows to many thousands.
        self.tree.clear()
        total = 0
        for kind, folder in self._roots():
            for path, key, size in stored_files(folder):
                total += size
                source = SOURCES[key].label if key in SOURCES else key
                item = QTreeWidgetItem([path.name, source, kind, _human_size(size)])
                item.setData(0, Qt.UserRole, (str(path), kind))
                item.setToolTip(0, str(path))
                self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        count = self.tree.topLevelItemCount()
        self.summary.setText(f"{count} file(s), {_human_size(total) or '0 B'}")

    def delete_selected(self) -> None:
        self._delete([item.data(0, Qt.UserRole) for item in self.tree.selectedItems()])

    def delete_all(self) -> None:
        self._delete([self.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.tree.topLevelItemCount())])

    def _delete(self, entries: list[tuple[str, str]]) -> None:
        if not entries:
            return
        # ponytail: any active job holds back every temporary file; check job params if that bites.
        if any(kind == _TEMPORARY for _path, kind in entries) and self._jobs_active():
            entries = [entry for entry in entries if entry[1] != _TEMPORARY]
            QMessageBox.information(self, "Downloads", "Temporary files stay while jobs are running: an import may still read them.")
            if not entries:
                return
        confirm = QMessageBox.question(
            self, "Downloads", f"Delete {len(entries)} downloaded file(s) from disk?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for kind, folder in self._roots():
            delete_files([path for path, entry_kind in entries if entry_kind == kind], folder)
        self.refresh()

    def _jobs_active(self) -> bool:
        try:
            return bool(self.runtime.list_jobs(statuses=NON_TERMINAL_JOB_STATUSES))
        except Exception:  # noqa: BLE001 — no open project, no jobs
            return False


__all__ = ["DownloadsPage"]
