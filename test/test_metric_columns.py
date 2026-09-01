"""Result tables: every column is filled, the opt-in ones are just hidden."""
from pathlib import Path
import sys

import pydantic.fields  # noqa: F401 - before PySide6: shiboken breaks pydantic's lazy imports
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QTableWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.ui.workspace import _MetricColumns


def test_metric_columns_fill_and_toggle():
    QApplication.instance() or QApplication(["amdockvs-columns-test"])
    table = QTableWidget()
    columns = _MetricColumns(table, (
        ("Pose", lambda ctx: f"#{ctx}", True),
        ("Score", lambda ctx: f"{ctx * -1.5:.2f}", True),
        ("LE", lambda ctx: "-0.31", False),
    ))
    table.setRowCount(1)
    columns.fill(0, 2)

    # Hidden columns still carry their value: turning one on must not need a reload.
    assert [table.item(0, c).text() for c in range(3)] == ["#2", "-3.00", "-0.31"]
    assert table.isColumnHidden(2)

    columns._set_visible(2, True)
    assert not table.isColumnHidden(2)
