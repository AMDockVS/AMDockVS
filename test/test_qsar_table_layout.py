"""Prediction tables distinguish ligand labels from numeric scores."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from amdockvs.ui.tools.qsar import panels


def test_prediction_table_aligns_scores_and_formats_classes(monkeypatch):
    app = QApplication.instance() or QApplication([])
    model = SimpleNamespace(
        id=7,
        name="Mock classifier",
        metrics={"task": "classification", "feature_kind": "ecfp4", "positive_class": 1},
    )
    rows = [{"molecule_id": 23, "name": "ligand_alpha", "value": 1.0,
             "confidence": 0.91, "positive_probability": 0.91}]
    runtime = SimpleNamespace(qsar=SimpleNamespace(
        list_models=lambda: [model],
        prediction_rows=lambda **_kwargs: rows,
    ))
    monkeypatch.setattr(panels, "run_async", lambda work, done, **_kwargs: done(work()))

    widget = panels.PredictionsWidget(runtime=runtime)
    try:
        app.processEvents()
        assert [widget.table.horizontalHeaderItem(c).text() for c in range(5)] == [
            "Ligand", "Prediction", "Confidence", "P(class 1)", "ID"]
        assert widget.table.columnWidth(0) == 300
        assert widget.table.horizontalHeader().sectionResizeMode(0) == panels.QHeaderView.Interactive
        assert not widget.table.verticalHeader().isVisible()
        assert widget.table.item(0, 0).text() == "ligand_alpha"
        assert widget.table.item(0, 0).toolTip() == "ligand_alpha"
        assert widget.table.item(0, 1).text() == "1"
        for column in (1, 2, 3, 4):
            assert widget.table.item(0, column).textAlignment() == Qt.AlignRight | Qt.AlignVCenter
        assert widget.table.item(0, 0).textAlignment() == Qt.AlignLeft | Qt.AlignVCenter
    finally:
        widget.close()
