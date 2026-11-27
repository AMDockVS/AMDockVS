"""The Activity table's model: rows render per endpoint kind, edits write through, and adding
ligands skips the ones already listed."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from amdockvs.ui.catalog.activity import _ActivityModel  # noqa: E402
from amdockvs.ui.tools.qsar.chart import ClassCounts, class_counts  # noqa: E402

app = QApplication.instance() or QApplication([])


def test_values_render_by_endpoint_kind():
    model = _ActivityModel(lambda *_a: True)
    model.set_rows([[7, "a", 1.0, "", "train"], [9, "b", 0.0, "", "test"]], "categorical")
    assert model.rowCount() == 2 and model.columnCount() == 5
    assert model.index(0, 2).data() == "1" and model.index(1, 4).data() == "test"
    model.set_rows([[7, "a", 6.25, "nM", ""]], "continuous")
    assert model.index(0, 2).data() == "6.2500" and model.index(0, 3).data() == "nM"
    assert not model.flags(model.index(0, 0)) & Qt.ItemIsEditable
    assert model.flags(model.index(0, 2)) & Qt.ItemIsEditable


def test_an_edit_writes_through_and_a_refused_one_keeps_the_cell():
    writes = []
    accept = {"ok": True}
    model = _ActivityModel(lambda row, value, unit: writes.append((row, value, unit)) or accept["ok"])
    model.set_rows([[7, "a", 1.5, "uM", ""]], "continuous")
    assert model.setData(model.index(0, 2), "2.5")
    assert writes[-1] == (0, "2.5", "uM") and model.index(0, 2).data() == "2.5000"
    assert model.setData(model.index(0, 3), "nM")  # a unit edit re-records the current value
    assert writes[-1] == (0, "2.5000", "nM")
    accept["ok"] = False
    assert not model.setData(model.index(0, 2), "9")
    assert model.index(0, 2).data() == "2.5000"
    assert not model.setData(model.index(0, 1), "renamed")  # the name is not editable


def test_a_unit_typed_before_the_value_is_kept_and_records_that_value():
    """A row added from the ligand picker carries the combo's unit and no value. Typing the unit
    first must keep it: it is what the value typed next is recorded in."""
    writes = []
    model = _ActivityModel(lambda row, value, unit: writes.append((row, value, unit)) or True)
    model.append(12, "new", "nM")
    assert model.setData(model.index(0, 3), "uM")  # no value yet: nothing to write, but it sticks
    assert writes == [] and model.index(0, 3).data() == "uM"
    assert model.setData(model.index(0, 2), "4")
    assert writes[-1] == (0, "4", "uM")  # the typed unit, not the combo's "nM"


def test_added_ligands_start_empty_and_ids_are_kept():
    model = _ActivityModel(lambda *_a: True)
    model.set_rows([[7, "a", 1.0, "", ""]], "continuous")
    model.append(12, "new", "nM")
    assert model.rowCount() == 2 and model.molecule_id(1) == 12 and model.molecule_ids() == {7, 12}
    assert model.index(1, 2).data() == "" and model.index(1, 3).data() == "nM"


def test_class_counts_are_one_bar_per_class():
    counts = class_counts([1.0, 0.0, 0.0, 1, 0, None])
    assert isinstance(counts, ClassCounts)
    assert counts == (["0", "1"], [3, 2])
