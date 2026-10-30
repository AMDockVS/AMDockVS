"""QSAR plots use the app palette and reset heatmap-only axis changes on reuse."""
import numpy as np
import pytest

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetricsF, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow
import pyqtgraph as pg

from ms_components.ms_dockwidget.widget import DockManager
from ms_components.theme import THEMES, apply_theme, current_theme
from amdockvs.ui.tools.qsar.chart import QSARChartDockWidget


@pytest.fixture(params=["catppuccin_mocha", "catppuccin_latte"])
def chart(request):
    app = QApplication.instance() or QApplication([])
    previous = next(name for name, palette in THEMES.items() if palette == current_theme())
    apply_theme(request.param, app)
    window = QMainWindow()
    dock = QSARChartDockWidget("Distribution", DockManager(window), window)
    window.addDockWidget(Qt.RightDockWidgetArea, dock)
    window.resize(700, 650)
    window.show()
    try:
        yield app, window, dock
    finally:
        window.close()
        apply_theme(previous, app)


def test_grouped_bars_use_distinct_theme_colors(chart):
    app, window, dock = chart
    dock.show_grouped_bars("Mock activity", ["0", "1"], {"train": [24, 18], "test": [6, 12]})
    app.processEvents()
    bars = [item for item in dock.chart_plot.getPlotItem().items if isinstance(item, pg.BarGraphItem)]
    colors = [item.opts["brush"].color() for item in bars]
    assert colors[0] == app.palette().color(QPalette.Highlight)
    assert colors[0] != colors[1]
    assert all(c.name() not in {"#ff0000", "#ffff00"} for c in colors)
    assert [list(item.opts["height"]) for item in bars] == [[24., 18.], [6., 12.]]


def test_heatmap_reserves_vertical_label_space_and_resets_for_scatter(chart):
    app, window, dock = chart
    labels = ["molecular_weight", "aromatic_ring_count", "fraction_csp3"]
    matrix = np.array([[1., -.6, .4], [-.6, 1., .2], [.4, .2, 1.]])
    dock.show_heatmap("Mock descriptors", labels, matrix)
    app.processEvents()
    window.grab()  # exercise painting, including rotated text
    plot = dock.universe_view.getPlotItem()
    axis = plot.getAxis("bottom")
    assert axis.rotated_labels == labels
    assert axis.height() >= max(QFontMetricsF(axis.font()).horizontalAdvance(s) for s in labels)
    image = next(item for item in plot.items if isinstance(item, pg.ImageItem))
    assert np.array_equal(image.image, matrix)
    assert list(image.getLevels()) == [-1., 1.]
    scale = dock._correlation_scale
    assert scale.isVisible() and not scale.interactive
    assert scale.axis.labelText == ""  # main tick axis is right; the title is on the left
    assert scale.getAxis("left").labelText == "Correlation (r)"
    assert scale.levels() == (-1., 1.)
    assert list(image.lut[0, :3]) == [33, 102, 172]  # negative: blue
    assert list(image.lut[-1, :3]) == [178, 24, 43]  # positive: red
    assert list(scale.colorMap().getLookupTable(nPts=257)[128, :3]) == [255, 255, 255]
    # Repeated previews update one scale, keeping the full correlation range.
    dock.show_heatmap("Positive-only correlations", labels, np.abs(matrix))
    app.processEvents()
    assert dock._correlation_scale is scale
    assert scale.levels() == (-1., 1.)
    assert scale.axis._tickLevels == [[(-1., "−1"), (-.5, "−0.5"), (0., "0"), (.5, "0.5"), (1., "1")]]
    for width, height in ((700, 900), (900, 500), (500, 700)):
        window.resize(width, height)
        for _ in range(5):
            app.processEvents()
        current_image = dock._correlation_image
        visible = current_image.mapRectToScene(current_image.boundingRect()).intersected(
            plot.getViewBox().sceneBoundingRect())
        gradient = scale.bar.mapRectToScene(scale.bar.boundingRect())
        assert abs(gradient.top() - visible.top()) < 1.5
        assert abs(gradient.bottom() - visible.bottom()) < 1.5
    dock.show_universe([[0., 0.], [1., 1.]], [], [.6, .2])
    app.processEvents()
    assert axis.rotated_labels is None
    assert axis.style["showValues"]
    assert axis.height() < 60
    assert not scale.isVisible()
    assert not scale.img_list  # scatter reuse must release the old heatmap


def test_scatter_has_fitted_and_distinct_ideal_lines(chart):
    app, window, dock = chart
    dock.show_scatter([1., 2., 3.], [3., 5., 7.], "mock")
    app.processEvents()
    lines = [i for i in dock.chart_plot.getPlotItem().items if isinstance(i, pg.PlotDataItem)]
    assert len(lines) == 2
    x, y = lines[0].getData()
    np.testing.assert_allclose(y, 2 * x + 1)
    x, y = lines[1].getData()
    np.testing.assert_array_equal(x, y)
    labels = [label.text for _, label in dock._chart_legend.items]
    assert "Fit: y = 2x +1" in labels
    assert "Ideal: y = x" in labels
    dock.show_scatter([1., 1.], [2., 3.])
    lines = [i for i in dock.chart_plot.getPlotItem().items if isinstance(i, pg.PlotDataItem)]
    assert len(lines) == 1  # no identifiable fitted slope for constant observed values


def test_confusion_counts_orientation_and_curve_reset(chart):
    app, window, dock = chart
    counts = [[8, 2], [3, 7]]
    dock.show_confusion_matrix("Mock test, n=20", [0, 1], counts)
    app.processEvents()
    window.grab()
    plot = dock.chart_plot.getPlotItem()
    image = next(i for i in plot.items if isinstance(i, pg.ImageItem))
    np.testing.assert_array_equal(image.image, counts)
    assert image.axisOrder == "row-major"
    assert plot.getViewBox().state["yInverted"]
    assert plot.getAxis("left").labelText == "Actual class"
    assert plot.getAxis("bottom").labelText == "Predicted class"
    cells = {i.textItem.toPlainText(): (i.pos().x(), i.pos().y())
             for i in plot.items if isinstance(i, pg.TextItem)}
    assert cells == {"TN\n8": (.5, .5), "FP\n2": (1.5, .5),
                     "FN\n3": (.5, 1.5), "TP\n7": (1.5, 1.5)}
    dock.show_curves("PR", [{"label": "Mock AP=.8", "points": [(0., 1.), (1., .5)],
                             "prevalence": .5}], "Recall", "Precision")
    app.processEvents()
    assert not plot.getViewBox().state["yInverted"]
    assert plot.getViewBox().state["aspectLocked"] is False
    assert not any(isinstance(i, pg.ImageItem) for i in plot.items)
    lines = [i for i in plot.items if isinstance(i, pg.PlotDataItem)]
    np.testing.assert_array_equal(lines[1].getData()[1], [.5, .5])


def test_classification_curve_legends_can_use_lower_corners(chart):
    app, _window, dock = chart
    curves = [{"label": "Mock model", "points": [(0., 0.), (1., 1.)]}]
    dock.show_curves("ROC", curves, "False positive rate", "True positive rate",
                     diagonal=True, legend_position="bottom_right")
    app.processEvents()
    assert tuple(dock._chart_legend.opts["offset"]) == (-10, -10)
    dock.show_curves("Precision-recall", curves, "Recall", "Precision",
                     legend_position="bottom_left")
    app.processEvents()
    assert tuple(dock._chart_legend.opts["offset"]) == (10, -10)


def test_redraw_after_theme_change_updates_chart_background(chart):
    app, window, dock = chart
    apply_theme("catppuccin_latte" if app.palette().color(QPalette.Window).lightness() < 128
                else "catppuccin_mocha", app)
    dock.show_scatter([1, 2], [2, 3])
    assert dock.chart_plot.backgroundBrush().color() == app.palette().color(QPalette.Window)
    dock.show_heatmap("Correlation", ["a", "b"], [[1., .5], [.5, 1.]])
    assert dock.universe_view.backgroundBrush().color() == app.palette().color(QPalette.Window)
