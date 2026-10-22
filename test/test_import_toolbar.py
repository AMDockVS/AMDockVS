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
