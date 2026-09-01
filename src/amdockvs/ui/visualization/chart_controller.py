"""The 2D plots that share PyMOL's dock region: distributions, ROC, heatmaps, the chemical
universe scatter and the glowing-molecule map."""

from __future__ import annotations

from PySide6.QtCore import QTimer

from amdockvs.ui.async_query import run_async
from amdockvs.ui.catalog.domain_views import LIGAND_ACTIVITY_VIEW_ID
from amdockvs.ui.tools.molecules.diversity import SELECTION_VIEW_ID
from amdockvs.ui.tools.qsar.chart import render_structure_png
from amdockvs.ui.tools.qsar.panels import PREDICTIONS_VIEW_ID, QSAR_MODELS_VIEW_ID


class ChartController:
    def __init__(self, window):
        self.w = window
        # Hovering a centroid marker previews its molecule; debounce so a sweep across markers
        # renders only where the cursor settles, and render off-thread (DB read + depiction).
        self._hover_molecule_id: int | None = None
        self._last_rendered_hover: int | None = None
        self._hover_timer = QTimer(window)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(140)
        self._hover_timer.timeout.connect(self._render_hovered_molecule)

    @property
    def _chart(self):
        return getattr(self.w, "qsar_chart_dock", None)

    def _show(self, dock_id: str = "qsar_chart") -> None:
        self.w.dock_manager.toggle(dock_id, True)

    def sync_to_active_view(self, view_id: str | None) -> None:
        """On the Activity view, show the distribution chart (hides PyMOL via EXCLUSIVE region);
        on any other view, hide it and bring PyMOL back."""
        if self._chart is None:
            return
        if view_id == LIGAND_ACTIVITY_VIEW_ID:
            widget = self.w.central_widget.open_view(view_id)
            if widget is not None and hasattr(widget, "refresh"):
                widget.refresh()  # _fill -> show_activity_histogram toggles the chart on
            return
        if view_id == SELECTION_VIEW_ID:
            widget = self.w.central_widget.open_view(view_id)
            if widget is not None and hasattr(widget, "restore_plot"):
                widget.restore_plot()  # re-pushes the last universe (or a placeholder) + toggles chart on
            return
        if view_id in (QSAR_MODELS_VIEW_ID, PREDICTIONS_VIEW_ID):
            return  # chart/glow show on demand (plot + glowing-molecule + row-select); leave state
        self.w.dock_manager.toggle("qsar_chart", False)
        if getattr(self.w, "qsar_glow_dock", None) is not None:
            self.w.dock_manager.toggle("qsar_glow", False)
        if self.w.pymol_dock is not None:
            self.w.dock_manager.toggle("pymol", True)
        # Big-data hygiene: the universe scatter can be heavy, so free it whenever we leave the
        # Diversity view. Its (small) point data stays in the widget → restore_plot redraws on return.
        self._chart.show_universe([], [], [0.0, 0.0])
        self._chart.hide_hover_structure()

    def show_activity_histogram(self, endpoint: str, bins) -> None:
        """bins = (labels, counts), already computed off the GUI thread by the caller."""
        if self._chart is None:
            return
        labels, counts = bins
        self._chart.show_distribution_bins(endpoint or "value", labels, counts)
        self._show()

    def show_feature_importance(self, label: str, pairs) -> None:
        """pairs = [(feature, importance), ...] (already sorted). Horizontal bars in PyMOL's region."""
        if self._chart is None or not pairs:
            return
        pairs = list(pairs)[:15]  # already biggest-first; show_hbars renders index 0 at the top
        self._chart.show_hbars(
            f"Feature importance ({label})" if label else "Feature importance",
            [p[0] for p in pairs], [p[1] for p in pairs], "importance",
        )
        self._show()

    def show_roc_curves(self, curves) -> None:
        """curves = [{'label', 'points':[(fpr,tpr)], 'auc'}]. Overlaid ROC series + chance diagonal."""
        if self._chart is None or not curves:
            return
        title = "ROC" if len(curves) > 1 else f"ROC — {curves[0].get('label', '')}"
        self._chart.show_curves(title, curves, "False positive rate", "True positive rate", diagonal=True)
        self._show()

    def show_model_fit_series(self, series) -> None:
        """series = [{'label','points':[(observed,predicted)]}]. Overlaid model-fit scatter."""
        if self._chart is None or not series:
            return
        title = "Observed vs predicted" if len(series) > 1 else f"Observed vs predicted — {series[0].get('label', '')}"
        self._chart.show_scatter_series(title, series)
        self._show()

    def show_correlation_heatmap(self, label: str, labels, matrix) -> None:
        """Descriptor correlation heatmap (pyqtgraph) in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_heatmap(label or "Descriptor correlation", labels, matrix)
        self._show()

    def show_split_distribution(self, label: str, categories, groups) -> None:
        """Train-vs-test label distribution grouped bars (split preview) in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_grouped_bars(label or "Train/test distribution", categories, groups)
        self._show()

    def show_similarity_distribution(self, label: str, bins) -> None:
        """bins = (labels, counts) of applicability-domain similarities, binned off the GUI thread."""
        if self._chart is None:
            return
        labels, counts = bins
        self._chart.show_distribution_bins(label or "AD similarity", labels, counts)
        self._show()

    def show_size_distribution(self, label: str, labels, counts) -> None:
        """Cluster-size distribution histogram for the Diversity view, in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_distribution_bins(label or "cluster size", labels, counts)
        self._show()

    def show_diversity_universe(
            self, pool_points, selection_groups, evr=(0.0, 0.0), highlight_points=None
    ) -> None:
        """Accumulated chemical-universe scatter for the Diversity view, drawn in PyMOL's region.
        Empty inputs draw a placeholder (tab entry with no preview yet / after a merge).
        ``highlight_points`` emphasises one cluster's centroid(s) and dims the rest."""
        if self._chart is None:
            return
        self._chart.show_universe(pool_points, selection_groups, evr, highlight_points)
        self._show()

    def show_glowing_molecule(self, molblock: str, weights, caption: str = "") -> None:
        if getattr(self.w, "qsar_glow_dock", None) is None:
            return
        self.w.qsar_glow_dock.show_glowing(molblock, weights, caption)
        self._show("qsar_glow")

    # -- centroid hover preview ----------------------------------------------------

    def on_universe_hover(self, molecule_id: int) -> None:
        if int(molecule_id) < 0:  # cursor left every marker
            self._hover_timer.stop()
            self._hover_molecule_id = None
            self._last_rendered_hover = None
            self._chart.hide_hover_structure()
            return
        self._hover_molecule_id = int(molecule_id)
        self._hover_timer.start()  # coalesce rapid marker-to-marker movement

    def _render_hovered_molecule(self) -> None:
        mid = self._hover_molecule_id
        if mid is None or mid == self._last_rendered_hover:
            return
        self._last_rendered_hover = mid

        def _work():
            molblock = self.w.runtime.selection.molblock_for(mid)
            return render_structure_png(molblock, legend=f"centroid #{mid}") if molblock else b""

        run_async(_work, self._show_hovered_structure, on_error=lambda _e: None)

    def _show_hovered_structure(self, png_bytes: bytes) -> None:
        if png_bytes:
            self._chart.show_hover_structure(png_bytes)
