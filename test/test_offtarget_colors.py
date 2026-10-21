import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import AliasChoices, BaseModel, Field
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from amdockvs.ui.tools.docking.offtarget import OffTargetResultsWidget


@pytest.fixture
def matrix(monkeypatch):
    app = QApplication.instance() or QApplication(["offtarget-colors-test"])
    monkeypatch.setattr(OffTargetResultsWidget, "refresh", lambda self: None)
    monkeypatch.setattr(OffTargetResultsWidget, "_pymol", lambda self: (None, None))
    widget = OffTargetResultsWidget(runtime=object())
    widget._columns = [(1, "Reference"), (2, "Off-target")]
    widget._row_state = {0: {
        "ligand_id": 1,
        "ligand_name": "Ligand",
        "poses_by_receptor": {
            1: [{"pose_rank": 1, "score": -10.0, "ligand_efficiency": -0.5},
                {"pose_rank": 2, "score": -8.0, "ligand_efficiency": -0.4}],
            2: [{"pose_rank": 1, "score": -5.0, "ligand_efficiency": -0.25}],
        },
    }}
    widget.selector_table.setColumnCount(3)
    widget.selector_table.setRowCount(1)
    widget.difference_table.setColumnCount(2)
    widget.difference_table.setRowCount(1)
    widget._render_row(0)
    yield widget
    widget.close()
    app.processEvents()


@pytest.mark.parametrize("diff, color", [
    (-8, "#2166ac"), (-5, "#2166ac"), (-2.5, "#90b2d5"),
    (0, "#ffffff"), (2.5, "#d88b95"), (5, "#b2182b"), (8, "#b2182b"),
])
def test_difference_background_interpolates_and_saturates(matrix, diff, color):
    matrix._row_state[0]["poses_by_receptor"][2][0]["score"] = -10 - diff
    matrix._render_difference_row(0)
    item = matrix.difference_table.item(0, 1)
    assert item.text() == f"{diff:+.2f}"
    assert item.background().color().getRgbF() == pytest.approx(QColor(color).getRgbF(), abs=1 / 255)
    background = item.background().color()
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                for c in background.getRgbF()[:3]]
    luminance = sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))
    contrast = ((luminance + 0.05) / 0.05 if item.foreground().color() == QColor("black")
                else 1.05 / (luminance + 0.05))
    assert contrast >= 4.5


def test_palette_change_preserves_pose_selection_and_recolors(matrix):
    combo = matrix.selector_table.cellWidget(0, 1)
    combo.setCurrentIndex(1)
    old_color = matrix.difference_table.item(0, 1).background().color()
    matrix.palette_combo.setCurrentIndex(1)
    assert matrix.selector_table.cellWidget(0, 1) is combo
    assert combo.currentData() == 2
    assert matrix.difference_table.item(0, 1).text() == "-3.00"
    assert matrix.difference_table.item(0, 1).background().color() != old_color
    assert "#762a83" in matrix.color_bar.styleSheet()


@pytest.mark.parametrize("score", [None, float("nan"), float("inf")])
def test_missing_or_nonfinite_values_have_no_color(matrix, score):
    matrix._row_state[0]["poses_by_receptor"][2][0]["score"] = score
    matrix._render_difference_row(0)
    item = matrix.difference_table.item(0, 1)
    assert item.text() == "—"
    assert item.background().style() == Qt.NoBrush


def test_metric_change_updates_values_and_legend_units(matrix):
    matrix.metric_combo.setCurrentIndex(1)
    assert matrix.difference_table.item(0, 1).text() == "-0.25"
    assert "kcal/mol/heavy atom" in matrix.color_scale.text()
