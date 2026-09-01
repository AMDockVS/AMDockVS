"""A dock that shares PyMOL's region (EXCLUSIVE): showing it hides PyMOL, and vice versa.

All charts render with **pyqtgraph** (the app's single chart backend). Two PlotWidgets live in a
QStackedWidget: ``chart_plot`` for the bar / curve / scatter fits, ``universe_view`` for the
accumulating 'chemical universe' scatter and the correlation heatmap (which locks aspect ratio /
inverts Y, so it keeps its own plot to avoid fighting the fit charts). Each show_* swaps the
visible page.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from ms_components.ms_dockwidget.widget import DockManager, MSDockWidget

pg.setConfigOptions(antialias=True)
_AXIS_GREY = (130, 130, 130)  # readable on both light and dark themes
_SINGLE = (80, 140, 210)      # single-series bars/scatter
_DASH = pg.mkPen((150, 150, 150), style=Qt.DashLine)


def histogram(values, bins: int = 10) -> tuple[list[str], list[int]]:
    """Bin values into (labels, counts). Empty -> ([], []); a single value -> one bin."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return [], []
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [f"{lo:.2g}"], [len(vals)]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    labels = [f"{lo + i * width:.2g}" for i in range(bins)]
    return labels, counts


class QSARChartDockWidget(MSDockWidget):
    universeHovered = Signal(int)  # a centroid marker was hovered → molecule_id (owner renders it)

    def __init__(self, title: str, dock_manager: DockManager, parent: QWidget | None = None):
        super().__init__(title, dock_manager, parent)
        # Floating structure preview shown on hover — a top-level tooltip window so it doesn't steal
        # the scatter's dock region (glow/pymol/chart all share it EXCLUSIVE).
        self._hover_label = QLabel(None, Qt.ToolTip)
        self._hover_label.setStyleSheet("QLabel { background: #ffffff; border: 1px solid #888; }")
        self.stack = QStackedWidget(self)
        self.chart_plot = pg.PlotWidget(background=None)  # bar / curve / scatter fits
        self.universe_view = pg.PlotWidget(background=None)  # chemical universe + heatmap
        for view in (self.chart_plot, self.universe_view):
            for name in ("left", "bottom"):
                axis = view.getPlotItem().getAxis(name)
                axis.setPen(pg.mkPen(_AXIS_GREY))
                axis.setTextPen(pg.mkPen(_AXIS_GREY))
        self._chart_legend = self.chart_plot.getPlotItem().addLegend(offset=(-10, 10), labelTextColor=_AXIS_GREY)
        # Fit the dock's width instead of imposing our own: the QStackedWidget's width hint is the
        # WIDEST child, which could inflate the dock past its area and spill the plot off the right
        # edge. Ignored horizontal = "take whatever width you're given"; a small minimum still lets
        # it shrink. Vertical stays Expanding.
        for view in (self.chart_plot, self.universe_view):
            view.setMinimumSize(80, 80)
            view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        universe = self.universe_view.getPlotItem()
        self._universe_legend = universe.addLegend(offset=(-10, 10), labelTextColor=_AXIS_GREY)
        self.stack.addWidget(self.chart_plot)
        self.stack.addWidget(self.universe_view)
        self.setWidget(self.stack)

    @staticmethod
    def _color(index: int, total: int):
        return pg.intColor(index, hues=max(total, 6))

    def _chart(self):
        """Switch to the fit-chart page and return a cleared PlotItem (ticks + title reset so a
        previous chart's category ticks don't bleed through)."""
        self.stack.setCurrentWidget(self.chart_plot)
        plot = self.chart_plot.getPlotItem()
        plot.clear()
        self._chart_legend.clear()
        plot.setTitle(None)
        for name in ("left", "bottom"):
            plot.getAxis(name).setTicks(None)
        plot.enableAutoRange()
        return plot

    def show_distribution(self, label: str, values) -> None:
        self.show_distribution_bins(label, *histogram(values))

    def show_distribution_bins(self, label: str, labels, counts) -> None:
        """Render a precomputed histogram (labels, counts). The binning is done off the GUI thread
        by the caller so a big endpoint never freezes the UI here."""
        plot = self._chart()
        plot.setTitle(f"Count vs {label}" if label else "Count", color=_AXIS_GREY)
        counts = [float(c) for c in counts]
        if counts:
            xs = list(range(len(counts)))
            plot.addItem(pg.BarGraphItem(x=xs, height=counts, width=0.8, brush=pg.mkBrush(_SINGLE), pen=None))
            if len(labels) == len(counts):
                plot.getAxis("bottom").setTicks([[(i, str(labels[i])) for i in xs]])
        plot.setLabel("bottom", label or "")
        plot.setLabel("left", "Count")

    def show_grouped_bars(self, title: str, categories, groups) -> None:
        """Grouped bars: categories on X, one bar-set per group. Used for train-vs-test balance —
        groups = {'train': [...], 'test': [...]} aligned to categories (classes or value bins)."""
        plot = self._chart()
        names = list(groups.keys())
        categories = list(categories)
        xs = list(range(len(categories)))
        count = len(names) or 1
        width = 0.8 / count
        for gi, name in enumerate(names):
            offs = [x + (gi - (count - 1) / 2) * width for x in xs]
            bar = pg.BarGraphItem(
                x=offs, height=[float(v) for v in groups[name]], width=width,
                brush=pg.mkBrush(self._color(gi, count)), pen=None,
            )
            plot.addItem(bar)
            self._chart_legend.addItem(bar, str(name))
        plot.getAxis("bottom").setTicks([[(i, str(categories[i])) for i in xs]])
        plot.setTitle(title, color=_AXIS_GREY)
        plot.setLabel("left", "Count")

    def show_hbars(self, title: str, labels, values, x_title: str = "") -> None:
        """Horizontal bar chart (feature importance): labels top→bottom, biggest first."""
        plot = self._chart()
        labels = list(labels)
        values = [float(v) for v in values]
        if values:
            ys = list(range(len(values) - 1, -1, -1))  # index 0 at the top
            plot.addItem(pg.BarGraphItem(x0=0, y=ys, height=0.7, width=values, brush=pg.mkBrush(_SINGLE), pen=None))
            plot.getAxis("left").setTicks([[(ys[i], str(labels[i])) for i in range(len(labels))]])
        plot.setLabel("bottom", x_title or "importance")
        plot.setTitle(title, color=_AXIS_GREY)

    def show_curves(self, title: str, curves, x_title: str, y_title: str, *, diagonal: bool = False) -> None:
        """Several line series overlaid with a legend: curves = [{'label', 'points':[(x,y)]}].
        Used to compare ROC curves of the ticked models on one axis."""
        plot = self._chart()
        curves = list(curves)
        for index, curve in enumerate(curves):
            points = curve.get("points", [])
            item = plot.plot(
                [float(x) for x, _ in points],
                [float(y) for _, y in points],
                pen=pg.mkPen(self._color(index, len(curves)), width=2),
            )
            self._chart_legend.addItem(item, str(curve.get("label", "")))
        if diagonal:
            ref = plot.plot([0.0, 1.0], [0.0, 1.0], pen=_DASH)
            self._chart_legend.addItem(ref, "chance")
        plot.setTitle(title, color=_AXIS_GREY)
        plot.setLabel("bottom", x_title)
        plot.setLabel("left", y_title)

    def show_scatter_series(self, title: str, series) -> None:
        """Observed-vs-predicted for several models overlaid: series = [{'label','points':[(obs,pred)]}].
        One scatter series per model (auto-coloured, legend on) + a shared y=x reference line."""
        plot = self._chart()
        series = list(series)
        allv: list[float] = []
        for index, spec in enumerate(series):
            points = spec.get("points", [])
            xs = [float(o) for o, _ in points]
            ys = [float(p) for _, p in points]
            allv += xs + ys
            item = pg.ScatterPlotItem(x=xs, y=ys, size=8, pen=None, brush=pg.mkBrush(self._color(index, len(series))))
            plot.addItem(item)
            self._chart_legend.addItem(item, str(spec.get("label", "")))
        if allv:
            lo, hi = min(allv), max(allv)
            plot.plot([lo, hi], [lo, hi], pen=_DASH)
        plot.setTitle(title, color=_AXIS_GREY)
        plot.setLabel("bottom", "Observed")
        plot.setLabel("left", "Predicted")

    def show_scatter(self, observed, predicted, label: str = "") -> None:
        """Observed-vs-predicted scatter with a y=x reference line (StarDrop's model-fit plot)."""
        plot = self._chart()
        xs = [float(o) for o in observed]
        ys = [float(p) for p in predicted]
        plot.addItem(pg.ScatterPlotItem(x=xs, y=ys, size=9, pen=None, brush=pg.mkBrush(_SINGLE)))
        allv = xs + ys
        if allv:
            lo, hi = min(allv), max(allv)
            plot.plot([lo, hi], [lo, hi], pen=_DASH)
        plot.setTitle(f"Observed vs predicted{f' ({label})' if label else ''}", color=_AXIS_GREY)
        plot.setLabel("bottom", "Observed")
        plot.setLabel("left", "Predicted")

    def show_universe(self, pool_points, selection_groups, evr=(0.0, 0.0), highlight_points=None) -> None:
        """Chemical-universe scatter (top-2 PCA axes, pyqtgraph) that ACCUMULATES across previews:
        ``pool_points`` is the grey cloud sampled so far, ``selection_groups`` a list of per-preview
        representative sets — ``[{"points": [(x, y), ...], "color": (r, g, b), "label": str}, ...]`` —
        each drawn in its own colour so successive previews stack up. All points must already be
        projected on the same PCA basis (fit once on the first preview). Empty inputs draw a 'press
        Preview' placeholder (tab entry / after a merge). ``highlight_points`` — when given, the pool
        and every centroid are dimmed and those points are drawn bright + ringed (a picked cluster)."""
        self.stack.setCurrentWidget(self.universe_view)
        evr = (list(evr or []) + [0.0, 0.0])[:2]
        groups = list(selection_groups or [])
        highlight = np.asarray(highlight_points, dtype=float) if len(highlight_points or []) else None
        dim = highlight is not None
        plot = self.universe_view.getPlotItem()
        plot.clear()
        plot.setAspectLocked(False)  # a prior heatmap locks aspect + inverts Y — undo for the scatter
        plot.invertY(False)
        self._universe_legend.clear()

        pool = np.asarray(pool_points, dtype=float) if len(pool_points or []) else np.empty((0, 2))
        if pool.size == 0 and not any(g.get("points") for g in groups):
            plot.setTitle("Chemical universe — press Preview to cluster the current scope.", color=_AXIS_GREY)
            return

        n_reps = sum(len(g.get("points") or []) for g in groups)
        pct = f"{evr[0] * 100:.1f}% + {evr[1] * 100:.1f}% = {(evr[0] + evr[1]) * 100:.1f}% variance"
        plot.setTitle(
            f"Chemical universe (PCA) — {pool.shape[0]} sampled · {n_reps} representatives "
            f"({len(groups)} previews) · {pct}",
            color=_AXIS_GREY,
        )
        plot.setLabel("bottom", f"PC1 ({evr[0] * 100:.1f}%)")
        plot.setLabel("left", f"PC2 ({evr[1] * 100:.1f}%)")

        if pool.shape[0]:
            item = pg.ScatterPlotItem(
                pos=pool, size=5, pen=None, brush=pg.mkBrush(150, 160, 175, 30 if dim else 140)
            )
            plot.addItem(item)
            self._universe_legend.addItem(item, f"pool ({pool.shape[0]})")
        # Coloured markers stand out, so keep them small — and shrink as previews pile up so a busy
        # plot doesn't turn into solid colour. When a cluster is picked, dim these to let it pop.
        rep_size = max(4.0, 8.0 - 0.5 * len(groups))
        rep_alpha = 45 if dim else 255
        for group in groups:
            pts = group.get("points") or []
            if not pts:
                continue
            r, g, b = group.get("color") or (214, 40, 40)
            item = pg.ScatterPlotItem(
                pos=np.asarray(pts, dtype=float), size=rep_size,
                pen=None, brush=pg.mkBrush(int(r), int(g), int(b), rep_alpha),
                data=list(group.get("ids") or []),  # per-point molecule_id for the hover preview
                hoverable=True, hoverSize=rep_size + 6, hoverPen=pg.mkPen("w", width=1.5),
                tip=None,  # suppress pyqtgraph's x/y/data tooltip — the molecule preview is the one place
            )
            item.sigHovered.connect(self._on_spot_hover)
            plot.addItem(item)
            self._universe_legend.addItem(item, group.get("label") or "representatives")
        if dim:  # the picked cluster's centroid(s): bright, larger, white-ringed, on top
            item = pg.ScatterPlotItem(
                pos=highlight, size=rep_size + 7, pen=pg.mkPen("w", width=2),
                brush=pg.mkBrush(255, 214, 10),
            )
            plot.addItem(item)
            self._universe_legend.addItem(item, f"picked cluster ({highlight.shape[0]})")
        plot.enableAutoRange()

    def show_heatmap(self, title: str, labels, matrix) -> None:
        """Correlation heatmap on the pyqtgraph view (diverging blue↔red colormap fixed to [-1, 1]
        so the colour means correlation directly). Axis tick labels only when few enough to read
        (≤30 features); for the ~200-wide RDKit block the pattern is the point."""
        self.stack.setCurrentWidget(self.universe_view)
        plot = self.universe_view.getPlotItem()
        plot.clear()
        self._universe_legend.clear()
        arr = np.asarray(matrix, dtype=float)
        if arr.ndim != 2 or arr.size == 0:
            plot.setTitle("No correlations to show.", color=_AXIS_GREY)
            return
        image = pg.ImageItem(arr)
        cmap = pg.colormap.get("CET-D1")  # diverging blue-white-red, ships with pyqtgraph
        if cmap is not None:
            image.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        image.setLevels((-1.0, 1.0))
        plot.addItem(image)
        plot.setTitle(f"{title} ({arr.shape[0]}×{arr.shape[0]})", color=_AXIS_GREY)
        labels = list(labels)
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            if arr.shape[0] <= 30 and len(labels) == arr.shape[0]:
                axis.setTicks([[(i + 0.5, labels[i]) for i in range(len(labels))]])
            else:
                axis.setTicks([])
        plot.setAspectLocked(True)
        plot.invertY(True)  # row 0 at the top, like a printed matrix
        plot.enableAutoRange()

    def _on_spot_hover(self, _item, points, *_a) -> None:
        """pyqtgraph hover → announce the hovered centroid's molecule_id (or hide the preview when the
        cursor leaves every marker). The owner (main window) resolves + renders the structure."""
        if len(points):
            data = points[0].data()
            if data is not None:
                self.universeHovered.emit(int(data))
                return
        self.universeHovered.emit(-1)  # left every marker → owner hides the preview

    def show_hover_structure(self, png_bytes: bytes) -> None:
        pixmap = QPixmap()
        if not png_bytes or not pixmap.loadFromData(png_bytes, "PNG"):
            return
        self._hover_label.setPixmap(pixmap)
        self._hover_label.resize(pixmap.size())
        # Keep the floating preview on-screen: place it down-right of the cursor, but flip to the left
        # / above when that would run off the edge of the current screen.
        pos = QCursor.pos()
        w, h = pixmap.width(), pixmap.height()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = pos.x() - w - 16 if pos.x() + 16 + w > geo.right() else pos.x() + 16
        y = pos.y() - h - 16 if pos.y() + 16 + h > geo.bottom() else pos.y() + 16
        self._hover_label.move(max(geo.left(), x), max(geo.top(), y))
        self._hover_label.show()

    def hide_hover_structure(self) -> None:
        self._hover_label.hide()


def render_structure_png(molblock: str, *, legend: str = "", size: tuple[int, int] = (240, 210)) -> bytes:
    """Plain 2-D depiction of a molecule (native RDKit Cairo, no model weights), with an optional
    caption under it — the hover preview, so all the hover info lives in this one image."""
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromMolBlock(molblock)
    if mol is None:
        raise ValueError("Unparseable molblock.")
    drawer = rdMolDraw2D.MolDraw2DCairo(*size)
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=str(legend))
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def render_glowing_png(molblock: str, weights, size: tuple[int, int] = (380, 320)) -> bytes:
    """RDKit similarity-map PNG (native Cairo, no matplotlib) coloring per-atom contributions."""
    from rdkit import Chem
    from rdkit.Chem.Draw import SimilarityMaps, rdMolDraw2D

    mol = Chem.MolFromMolBlock(molblock)
    if mol is None:
        raise ValueError("Unparseable molblock.")
    drawer = rdMolDraw2D.MolDraw2DCairo(*size)
    SimilarityMaps.GetSimilarityMapFromWeights(mol, list(weights), draw2d=drawer)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


class Glowing2DDockWidget(MSDockWidget):
    """2D structure with per-atom model contributions (StarDrop's glowing molecule). Shares
    PyMOL's region — there is no 2D editor yet, this is read-only interpretability."""

    LEGEND = (
        "<b>Per-atom contribution to the model's prediction.</b><br>"
        "<span style='color:#2a9d2a'>● green</span> = atom raises the predicted value "
        "(e.g. more potent) &nbsp;·&nbsp; "
        "<span style='color:#c0392b'>● red</span> = atom lowers it &nbsp;·&nbsp; "
        "stronger color = bigger effect. This is what the model learned, not measured truth."
    )

    def __init__(self, title: str, dock_manager: DockManager, parent: QWidget | None = None):
        super().__init__(title, dock_manager, parent)
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        self.label = QLabel("Select a model + ligand to show the glowing molecule.", container)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.legend = QLabel(self.LEGEND, container)
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.RichText)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.legend)
        self.setWidget(container)

    def show_glowing(self, molblock: str, weights, caption: str = "") -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(render_glowing_png(molblock, weights), "PNG")
        self.label.setPixmap(pixmap)
        self.label.setToolTip(caption)


def _demo() -> None:
    assert histogram([]) == ([], [])
    assert histogram([5, 5, 5]) == (["5"], [3])
    labels, counts = histogram([1, 1, 2, 3, 10], bins=5)
    assert sum(counts) == 5, counts
    assert counts[-1] == 1, counts  # the 10 lands in the top bin
    assert len(labels) == 5
    print("ok")


if __name__ == "__main__":
    _demo()
