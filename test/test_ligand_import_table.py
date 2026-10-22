"""The ligand import file list is a model/view table: thousands of files, no widget per row."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401  (pydantic before PySide6)

pytest.importorskip("PySide6")
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox

from amdockvs.models.molecules import MoleculeType
from amdockvs.ui.tools.importing.ligands import FileDropTable


def test_thousands_of_files_edit_type_and_reference_without_row_widgets(tmp_path):
    app = QApplication.instance() or QApplication([])
    table = FileDropTable()
    changes: list[int] = []
    table.rows_changed.connect(lambda: changes.append(1))
    paths = [str(tmp_path / f"ZINC{i:06d}.mol2") for i in range(3000)]

    table.add_files(paths)
    table.add_files(paths[:10])  # already listed: ignored
    assert table.model().rowCount() == 3000 and len(changes) == 1
    assert not table.findChildren(QComboBox)  # no combo until a Type cell is edited

    model = table.model()
    table.edit(model.index(1, 1))  # what a click on the Type cell does
    combo = table.findChild(QComboBox)
    combo.setCurrentIndex(combo.findData(MoleculeType.PROTEIN))
    combo.activated.emit(combo.currentIndex())
    app.processEvents()
    assert table.rows()[1][1] == MoleculeType.PROTEIN
    assert model.index(1, 1).data() == "Protein ▾"

    table.resize(800, 400)
    table.show()
    center = table.visualRect(model.index(2, 4)).center()  # a click in the cell, off the indicator
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, center)
    assert table.reference_paths() == {paths[2]} and changes[-1] == 1
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, center)
    assert table.reference_paths() == set()
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, center)
    assert table.reference_paths() == {paths[2]}

    selection = table.selectionModel()
    for row in (0, 2):
        selection.select(model.index(row, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    table.remove_selected()
    assert [path for path, _ in table.rows()[:2]] == [paths[1], paths[3]]
    assert table.reference_paths() == set() and table.has_small_molecule()


def test_a_folder_brings_in_its_ligand_files_recursively(tmp_path):
    QApplication.instance() or QApplication([])
    (tmp_path / "sub").mkdir()
    for name in ("a.mol2", "b.SDF", "notes.md", "sub/c.smi"):
        (tmp_path / name).touch()

    table = FileDropTable()
    table.add_files([str(tmp_path)])

    assert sorted(Path(path).name for path, _ in table.rows()) == ["a.mol2", "b.SDF", "c.smi"]
