"""The Activity tab offers the assay columns of every reference table, not only the first one.

Worth a test because the second file's assays were simply missing from the picker, and the
obvious fix (re-sniff on every change) would wipe the ticks the user already made.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from amdockvs.ui.tools.importing.ligand_forms import ImportActivityForm


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def tables(tmp_path):
    first = tmp_path / "activity.csv"
    first.write_text("SMILES,name,pIC50,active\nCCO,ethanol,5.1,1\nCCC,propane,6.2,0\n", encoding="utf-8")
    second = tmp_path / "second_activity.csv"
    second.write_text("SMILES,name,second_assay,active\nCCN,amine,4.5,0.7\n", encoding="utf-8")
    return str(first), str(second)


def test_every_reference_table_feeds_the_picker(qapp, tables):
    form = ImportActivityForm()
    form.set_source_files(list(tables))
    assert list(form._chips) == ["pIC50", "active", "second_assay"]
    assert form._detected_kinds["pIC50"] == "continuous"
    # 0/1 in the first file, 0.7 in the second: only continuous fits both.
    assert form._detected_kinds["active"] == "continuous"


def test_marking_another_file_keeps_the_ticks_already_made(qapp, tables):
    first, second = tables
    form = ImportActivityForm()
    form.set_source_files([first])
    assert form._detected_kinds["active"] == "categorical"
    form._chips["pIC50"].setChecked(True)

    form.set_source_files([first, second])
    assert form._chips["pIC50"].isChecked()
    assert not form._chips["second_assay"].isChecked()  # new columns come unticked

    policy: dict = {}
    form._chips["second_assay"].setChecked(True)
    form.contribute(policy)
    assert policy["activity_columns"] == ("pIC50", "second_assay")


def test_no_table_hides_the_picker(qapp, tables, tmp_path):
    form = ImportActivityForm()
    form.set_source_files(list(tables))
    sdf = tmp_path / "ligands.sdf"
    sdf.write_text("", encoding="utf-8")
    form.set_source_files([str(sdf)])
    assert form._chips == {} and form.columns_box.isHidden()


@pytest.mark.parametrize("suffix", [".smi", ".txt", ".tsv", ".csv"])
@pytest.mark.parametrize("separator", ["\t", ",", " "], ids=["tab", "comma", "space"])
def test_the_picker_reads_a_table_the_way_the_importer_does(qapp, tmp_path, suffix, separator):
    """Any accepted table suffix, any separator the importer recognizes: tab-separated .smi was empty."""
    table = tmp_path / f"activities{suffix}"
    rows = [["SMILES", "name", "IC50"], ["CCO", "ethanol", "100"], ["CCN", "amine", "250"]]
    table.write_text("".join(separator.join(row) + "\n" for row in rows), encoding="utf-8")
    form = ImportActivityForm()
    form.set_source_files([str(table)])
    assert form._detected_kinds == {"IC50": "continuous"}
