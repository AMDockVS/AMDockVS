"""Model Analysis actions stay accessible as the catalog pane changes width."""
from types import SimpleNamespace

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QScrollArea, QLabel, QPushButton

from amdockvs.ui.tools.qsar import panels


@pytest.mark.parametrize("width", [512, 650, 917, 1280])
@pytest.mark.parametrize("task", ["regression", "classification"])
def test_actions_wrap_without_horizontal_page_scrolling(monkeypatch, width, task):
    app = QApplication.instance() or QApplication([])
    model = SimpleNamespace(id=1, name="Mock ECFP4 model with a longer descriptive name",
        algorithm="logistic_regression", target="mock_active", source="trained",
        metrics={"task": task, "feature_kind": "ecfp4", "n_train": 40,
                 "train": {"n_samples": 40, "accuracy": .8 if task == "classification" else None,
                           "r2": .7 if task == "regression" else None},
                 "test": {"n_samples": 20, "mcc": .5 if task == "classification" else None,
                          "rmse": .4 if task == "regression" else None}})
    monkeypatch.setattr(panels, "run_async", lambda work, done, **kw: done(work()))
    widget = panels.QSARModelsWidget(runtime=SimpleNamespace(qsar=SimpleNamespace(list_models=lambda: [model])))
    widget.resize(width, 820)
    widget.show()
    try:
        assert widget.models_table.columnCount() == 8
        assert widget.models_table.horizontalHeaderItem(0).text() == "Model"
        assert widget.models_table.horizontalHeaderItem(6).text() == "Train n"
        assert widget.models_table.horizontalHeaderItem(7).text() == "Test n"
        assert widget.models_table.selectionMode() == panels.QAbstractItemView.ExtendedSelection
        assert all(not (widget.models_table.item(0, c).flags() & Qt.ItemIsUserCheckable)
                   for c in range(widget.models_table.columnCount()))
        assert widget.models_table.currentRow() == 0
        assert not hasattr(widget, "comparison_table")
        visible_text = [child.text() for child in widget.findChildren(QLabel) + widget.findChildren(QPushButton)]
        assert not any(text == "Use the model:" for text in visible_text)
        assert not any(text in {"Compare", "Predict all ligands", "Predict shards", "Promote top…", "Evaluate",
                                "Observed vs predicted"} for text in visible_text)
        for _ in range(5):
            app.processEvents()
        scroll = widget.findChild(QScrollArea)
        assert widget.width() == width
        assert scroll.horizontalScrollBar().maximum() == 0
        assert not scroll.horizontalScrollBar().isVisible()
        controls = list(widget._action_buttons.values()) + [widget.eval_subset]
        rectangles = []
        for control in controls:
            assert control.isVisible()
            rect = control.rect().translated(control.mapTo(scroll.widget(), QPoint(0, 0)))
            assert rect.left() >= 0 and rect.right() < scroll.widget().width()
            assert rect.bottom() < scroll.widget().height()
            assert all(not rect.intersects(previous) for previous in rectangles)
            rectangles.append(rect)
        assert widget.eval_subset.currentData() == "test"
        assert widget._action_buttons["roc"].isEnabled() == (task == "classification")
        assert widget._action_buttons["pr"].isEnabled() == (task == "classification")
        assert widget._action_buttons["confusion"].isEnabled() == (task == "classification")
        assert widget._action_buttons["glowing"].isEnabled()
        assert "Select a model row" not in widget.model_summary.text()
        assert "Holdout test" in widget.model_summary.text()
        if task == "classification":
            assert "Accuracy" in widget.model_summary.text()
            assert "ACCURACY" not in widget.model_summary.text()
        else:
            assert "RMSE" in widget.model_summary.text()
        assert not widget.models_table.item(0, 0).text().startswith("#")
        assert "Acting on:" not in widget.model_summary.text()
    finally:
        widget.close()


def test_selecting_rows_targets_actions_and_updates_summary(monkeypatch):
    app = QApplication.instance() or QApplication([])
    models = [
        SimpleNamespace(id=1, name="Classifier", algorithm="logistic_regression", target="active",
                        source="trained", metrics={"task": "classification", "feature_kind": "ecfp4",
                        "split": "random", "n_train": 8, "train": {"accuracy": .8},
                        "test": {"n_samples": 2, "mcc": .5}}),
        SimpleNamespace(id=2, name="Regressor", algorithm="ridge", target="affinity", source="trained",
                        metrics={"task": "regression", "feature_kind": "descriptors", "n_train": 8,
                                 "train": {"r2": .8}, "test": {"n_samples": 2, "rmse": .3}}),
    ]
    monkeypatch.setattr(panels, "run_async", lambda work, done, **kw: done(work()))
    widget = panels.QSARModelsWidget(runtime=SimpleNamespace(qsar=SimpleNamespace(list_models=lambda: models)))
    try:
        assert widget._selected_models() == [models[0]]
        assert widget._action_buttons["roc"].isEnabled()
        widget.models_table.selectRow(1)
        app.processEvents()
        assert widget._selected_models() == [models[1]]
        assert not widget._action_buttons["roc"].isEnabled()
        assert "Regressor" in widget.model_summary.text()
        assert "Training (n=8)" in widget.model_summary.text()
        assert not widget.model_summary.text().startswith("<b>#")
        from PySide6.QtCore import QItemSelectionModel
        widget.models_table.selectionModel().select(
            widget.models_table.model().index(0, 0),
            QItemSelectionModel.Select | QItemSelectionModel.Rows)
        widget.models_table.selectionModel().select(
            widget.models_table.model().index(1, 0),
            QItemSelectionModel.Select | QItemSelectionModel.Rows)
        app.processEvents()
        assert len(widget._selected_models()) == 2
    finally:
        widget.close()
