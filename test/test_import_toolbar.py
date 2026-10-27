"""The importers' add/remove bar is a vertical QToolBar showing icon and text, enabled with the scan."""
from __future__ import annotations


def test_receptor_import_toolbar_shows_icon_and_text():
    import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QToolBar

    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    QApplication.instance() or QApplication(["toolbar-test"])
    panel = ReceptorImportPanel(show_file_controls=True)
    (bar,) = panel.findChildren(QToolBar)
    assert bar.orientation() == Qt.Vertical and bar.toolButtonStyle() == Qt.ToolButtonTextUnderIcon
    assert [a.text() for a in bar.actions() if not a.isSeparator()] == ["Add files", "Add folder", "Add sequence", "Download", "Remove"]
    assert panel.add_files_action.isEnabled() and not panel.remove_files_action.isEnabled()  # nothing to remove yet


def test_receptor_folder_brings_in_its_structure_files(tmp_path, monkeypatch):
    import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    QApplication.instance() or QApplication(["toolbar-test"])
    (tmp_path / "panel").mkdir()
    for name in ("1abc.pdb", "panel/2xyz.CIF", "readme.md"):
        (tmp_path / name).touch()

    monkeypatch.setattr(ReceptorImportPanel, "start_scan", lambda self: None)  # no scan thread outliving the test
    panel = ReceptorImportPanel(show_file_controls=True)
    panel._add_paths([str(tmp_path)])  # what both "Add folder" and dropping the folder do

    assert sorted(Path(p).name for p in panel.file_paths) == ["1abc.pdb", "2xyz.CIF"]


def test_large_receptor_import_lists_files_without_processing(tmp_path, monkeypatch):
    import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing import receptors

    app = QApplication.instance() or QApplication(["toolbar-test"])

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("Large imports must only list files")

    monkeypatch.setattr(receptors, "scan_receptor_structure", unexpected_scan)
    monkeypatch.setattr(receptors, "build_receptor_import_preview", unexpected_scan)
    paths = [str(tmp_path / f"receptor_{index}.pdb") for index in range(1111)]
    panel = receptors.ReceptorImportPanel(file_paths=paths, show_file_controls=True)
    panel.resize(1140, 720)
    panel.show()
    app.processEvents()

    assert panel.table.isVisible() and panel.table.rowCount() == len(paths)
    assert [column for column in range(panel.table.columnCount()) if not panel.table.isColumnHidden(column)] == [0, 10, 11]
    for row, path in enumerate(paths):
        assert panel.table.item(row, 0).text() == f"receptor_{row}.pdb"
        assert panel.table.item(row, 10).text() == "PDB"
        assert panel.table.item(row, 11).text() == path
        assert not panel.table.item(row, 0).flags() & Qt.ItemIsEditable
        assert all(panel.table.cellWidget(row, column) is None for column in range(panel.table.columnCount()))
    assert panel.table.geometry().bottom() < panel.remove_non_structural_waters.geometry().top()
    assert panel.is_ready() and panel.remove_files_action.isEnabled()
    assert panel._scan_thread is None and panel.progress_bar.isHidden()
    panel.remove_cofactors.setChecked(True)
    request = panel.import_request()
    assert request["remove_cofactors"] is True
    assert request["build_specs"] is False and request["scans"] == {} and request["per_file"] == {}
    panel._on_remove_selected_files()  # no selection: retain the full list
    assert panel.file_paths == paths
    panel.table.selectRow(0)
    panel._on_remove_selected_files()
    assert panel.file_paths == paths[1:] and panel.table.rowCount() == 1110
    assert "1110 receptors selected" in panel.info_label.text()
    panel.close()


def test_receptor_removal_reenables_preview_at_fifty_files(tmp_path, monkeypatch):
    import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    app = QApplication.instance() or QApplication(["toolbar-test"])
    scans = []
    monkeypatch.setattr(ReceptorImportPanel, "start_scan", lambda self: scans.append(self.file_paths))
    paths = [str(tmp_path / f"receptor_{index}.cif") for index in range(51)]
    panel = ReceptorImportPanel(file_paths=paths, show_file_controls=True)
    app.processEvents()
    assert scans == []
    panel.table.selectRow(50)
    panel._on_remove_selected_files()
    app.processEvents()

    assert scans == [paths[:50]] and panel.table.rowCount() == 50
    assert not panel.table.isColumnHidden(1) and panel.table.isColumnHidden(11)
    assert panel.info_label.isHidden() and not panel.is_ready()
    assert not panel.remove_files_action.isEnabled()
    assert panel.import_request()["build_specs"] is True
    panel.set_files([])
    assert panel.table.rowCount() == 0 and not panel.is_ready()
    panel.close()


def test_switching_to_large_receptor_list_clears_preview_widgets(tmp_path):
    import amdockvs.core.configuration  # noqa: F401 - pydantic before PySide6
    from PySide6.QtWidgets import QApplication

    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    app = QApplication.instance() or QApplication(["toolbar-test"])
    paths = [str(tmp_path / f"receptor_{index}.pdb") for index in range(51)]
    panel = ReceptorImportPanel(file_paths=paths[:1])
    panel._ensure_row_widgets(0)
    assert panel.table.cellWidget(0, 1) is not None
    panel.set_files(paths)
    app.processEvents()  # the previously queued scan must not start for the new large batch

    assert panel.table.cellWidget(0, 1) is None
    assert panel._scan_thread is None and panel.is_ready()
    assert panel.table.rowCount() == 51 and panel.table.item(0, 11).text() == paths[0]
    panel.close()
