"""Redocking analysis panel: cumulative success curve, funnel (score vs RMSD) and rank
composition, plus a per-protocol summary table (SR@1Å/2Å, median, P90). Rendered with
**pyqtgraph** (already a dependency) — same backend as the chemical-universe view, so the app
doesn't mix chart libraries. All the maths lives in docking/redocking_metrics.py — this file
only builds curves/points/bars."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.docking.redocking_metrics import (
    case_rmsds,
    funnel_points,
    rank_composition,
    success_curve,
    summary_stats,
)

pg.setConfigOptions(antialias=True)
_AXIS_GREY = (130, 130, 130)  # readable on both light and dark themes
_SUCCESS_ANGSTROM = 2.0
_RANK_COLORS = ((46, 125, 50), (245, 166, 35), (183, 28, 28))  # rank1 green / rank2+ amber / fail red


class RedockingChartsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._records: list[dict] = []

        outer = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.chart_combo = QComboBox(self)
        self.chart_combo.addItems(["Cumulative success", "Funnel: score vs RMSD", "Rank composition"])
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItems(["Top-1", "Best-of-5", "Best-of-10"])
        controls.addWidget(QLabel("Chart", self))
        controls.addWidget(self.chart_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Poses", self))
        controls.addWidget(self.mode_combo)
        controls.addStretch(1)
        outer.addLayout(controls)

        self.plot_widget = pg.PlotWidget(background=None)
        plot = self.plot_widget.getPlotItem()
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setPen(pg.mkPen(_AXIS_GREY))
            axis.setTextPen(pg.mkPen(_AXIS_GREY))
        self._legend = plot.addLegend(offset=(-10, 10), labelTextColor=_AXIS_GREY)
        outer.addWidget(self.plot_widget, 1)

        self.summary_table = QTableWidget(0, 6, self)
        self.summary_table.setHorizontalHeaderLabels(["Protocol", "N", "SR@1Å", "SR@2Å", "Median", "P90"])
        self.summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setMaximumHeight(160)
        outer.addWidget(self.summary_table)

        self.chart_combo.currentIndexChanged.connect(self._render)
        self.mode_combo.currentIndexChanged.connect(self._render)

    def set_records(self, records: list[dict]) -> None:
        self._records = list(records or [])
        self._render()

    def _mode(self) -> tuple[str, int]:
        text = self.mode_combo.currentText()
        if text == "Top-1":
            return "top1", 1
        return "best", (5 if "5" in text else 10)

    @staticmethod
    def _color(index: int, total: int):
        return pg.intColor(index, hues=max(total, 6))

    def _plot(self):
        plot = self.plot_widget.getPlotItem()
        plot.clear()
        self._legend.clear()
        plot.setTitle(None)
        return plot

    def _message(self, text: str) -> None:
        plot = self._plot()
        plot.setTitle(text, color=_AXIS_GREY)

    def _render(self) -> None:
        self._render_summary()
        kind = self.chart_combo.currentText()
        if kind.startswith("Cumulative"):
            self._render_curve()
        elif kind.startswith("Funnel"):
            self._render_funnel()
        else:
            self._render_rank()

    def _render_curve(self) -> None:
        mode, n = self._mode()
        per = case_rmsds(self._records, mode=mode, n=n)
        if not per:
            self._message("No RMSD values to plot — run redocking first.")
            return
        x_max = max(2.5, min(max(max(v) for v in per.values()), 10.0))
        plot = self._plot()
        protocols = sorted(per)
        for index, protocol in enumerate(protocols):
            pts = success_curve(per[protocol], x_max=x_max)
            item = plot.plot(
                [p[0] for p in pts],
                [p[1] * 100.0 for p in pts],
                pen=pg.mkPen(self._color(index, len(protocols)), width=2),
            )
            self._legend.addItem(item, protocol)
        plot.addItem(pg.InfiniteLine(pos=_SUCCESS_ANGSTROM, angle=90, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
        plot.setTitle(f"Cumulative success vs RMSD threshold ({self.mode_combo.currentText()})", color=_AXIS_GREY)
        plot.setLabel("bottom", "RMSD threshold (Å)")
        plot.setLabel("left", "Success (%)")
        plot.setYRange(0.0, 100.0)
        plot.setXRange(0.0, x_max)

    def _render_funnel(self) -> None:
        pts = funnel_points(self._records)
        if not pts:
            self._message("No score/RMSD pairs to plot.")
            return
        plot = self._plot()
        protocols = sorted(pts)
        for index, protocol in enumerate(protocols):
            points = pts[protocol]
            item = pg.ScatterPlotItem(
                x=[p[0] for p in points],
                y=[p[1] for p in points],
                size=7,
                pen=None,
                brush=pg.mkBrush(self._color(index, len(protocols))),
            )
            plot.addItem(item)
            self._legend.addItem(item, protocol)
        plot.addItem(pg.InfiniteLine(pos=_SUCCESS_ANGSTROM, angle=90, pen=pg.mkPen((150, 150, 150), style=Qt.DashLine)))
        plot.setTitle("Funnel: docking score vs RMSD (all poses)", color=_AXIS_GREY)
        plot.setLabel("bottom", "RMSD vs reference (Å)")
        plot.setLabel("left", "Docking score (kcal/mol)")
        plot.enableAutoRange()

    def _render_rank(self) -> None:
        comp = rank_composition(self._records, threshold=_SUCCESS_ANGSTROM)
        if not comp:
            self._message("No cases to rank.")
            return
        protocols = sorted(comp)
        plot = self._plot()
        xs = list(range(len(protocols)))
        width = 0.25
        for group_index, label in enumerate(("Rank 1", "Rank 2+", "Fail")):
            brush = pg.mkBrush(_RANK_COLORS[group_index])
            bar = pg.BarGraphItem(
                x=[x + (group_index - 1) * width for x in xs],
                height=[comp[p][group_index] for p in protocols],
                width=width,
                brush=brush,
                pen=None,
            )
            plot.addItem(bar)
            self._legend.addItem(bar, label)
        plot.getAxis("bottom").setTicks([[(x, protocols[i]) for i, x in enumerate(xs)]])
        plot.setTitle(f"Near-native pose (≤{_SUCCESS_ANGSTROM:g} Å) rank per case", color=_AXIS_GREY)
        plot.setLabel("left", "Cases")
        plot.enableAutoRange()

    def _render_summary(self) -> None:
        mode, n = self._mode()
        per = case_rmsds(self._records, mode=mode, n=n)
        protocols = sorted(per)
        self.summary_table.setRowCount(len(protocols))
        for row, protocol in enumerate(protocols):
            stats = summary_stats(per[protocol], thresholds=(1.0, 2.0))
            cells = [
                protocol,
                str(stats.get("n", 0)),
                f"{stats.get('sr1', 0.0) * 100:.0f}%",
                f"{stats.get('sr2', 0.0) * 100:.0f}%",
                f"{stats.get('median', 0.0):.2f}",
                f"{stats.get('p90', 0.0):.2f}",
            ]
            for col, text in enumerate(cells):
                self.summary_table.setItem(row, col, QTableWidgetItem(text))
        self.summary_table.resizeColumnsToContents()
