"""Activity distributions respect endpoint types and preserve every measured value."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from amdockvs.ui.catalog import activity
from amdockvs.ui.tools.qsar.chart import histogram


@pytest.mark.parametrize("values,labels,counts", [
    ([0, 1, 0, None, 1, 0], ["0", "1"], [3, 2]),
    ([1, -1, 1, -1, -1], ["-1", "1"], [3, 2]),
    ([2, 0, 1, 2], ["0", "1", "2"], [1, 1, 2]),
    ([1, 1], ["1"], [2]),
    ([], [], []),
])
def test_categorical_distribution_counts_each_class(values, labels, counts):
    assert histogram(values, categorical=True) == (labels, counts)


def test_continuous_distribution_keeps_numeric_bins():
    labels, counts = histogram([4, 4.1, 4.2, 4.3, 5, 5.1, 5.2, 5.3, 6, 6.1, None])
    assert len(labels) == len(counts) == 10
    assert sum(counts) == 10
    assert labels[0] == "4" and labels[-1] == "5.9"
    # Even a sparse continuous endpoint keeps its numeric range.
    assert histogram([0, 1])[1] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 1]


@pytest.mark.parametrize("kind,expected", [
    ("categorical", (["0", "1"], [3, 2])),
    ("continuous", (["0", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"],
                    [3, 0, 0, 0, 0, 0, 0, 0, 0, 2])),
])
def test_activity_view_uses_endpoint_kind(monkeypatch, kind, expected):
    app = QApplication.instance() or QApplication([])
    rows = [dict(molecule_id=i, name=f"mock{i}", value=v, unit="")
            for i, v in enumerate([0, 1, 0, 1, 0], 1)]
    runtime = SimpleNamespace(qsar=SimpleNamespace(
        endpoint_kinds=lambda: {"mock": kind}, activity_rows=lambda **kw: rows,
        list_models=lambda: [],
    ))
    monkeypatch.setattr(activity, "run_async", lambda work, done, **kw: done(work()))
    charts = []
    widget = activity.LigandActivityWidget(runtime=runtime,
        show_histogram=lambda endpoint, bins: charts.append((endpoint, bins)))
    try:
        app.processEvents()
        assert charts == [("mock", expected)]
        assert widget.table.rowCount() == 5
        assert [widget.table.horizontalHeaderItem(c).text() for c in range(5)] == [
            "Ligand", "Unit", "Model split", "Activity", "ID"]
        assert widget.table.columnWidth(0) == 300
        assert widget.table.horizontalHeader().sectionResizeMode(0) == activity.QHeaderView.Interactive
        assert not widget.table.verticalHeader().isVisible()
        assert widget.table.item(0, 0).text() == "mock1"
        assert widget.table.item(0, 0).toolTip() == "mock1"
        assert widget.table.item(0, 3).textAlignment() == Qt.AlignRight | Qt.AlignVCenter
        assert widget.table.item(0, 4).textAlignment() == Qt.AlignRight | Qt.AlignVCenter
    finally:
        widget.close()


def test_activity_edit_uses_id_after_display_columns(monkeypatch):
    app = QApplication.instance() or QApplication([])
    rows = [dict(molecule_id=41, name="ligand_alpha", value=0.25, unit="uM")]
    saved = []
    runtime = SimpleNamespace(qsar=SimpleNamespace(
        endpoint_kinds=lambda: {"mock": "continuous"},
        activity_rows=lambda **_kwargs: rows,
        list_models=lambda: [],
        set_activity=lambda **kwargs: saved.append(kwargs),
    ))
    monkeypatch.setattr(activity, "run_async", lambda work, done, **_kw: done(work()))
    widget = activity.LigandActivityWidget(runtime=runtime)
    try:
        app.processEvents()
        widget.table.item(0, 3).setText("0.75")
        assert saved == [{"ligand_id": 41, "endpoint": "mock", "value": 0.75, "unit": "uM"}]
    finally:
        widget.close()
