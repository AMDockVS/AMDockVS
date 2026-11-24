"""The 2D plots that share PyMOL's dock region: distributions, ROC, heatmaps, the chemical
universe scatter and the glowing-molecule map."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication

from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.tools.qsar.chart import render_structure_png
from amdockvs.ui.view_ids import (
    CLUSTERS_VIEW_ID,
    LIGAND_ACTIVITY_VIEW_ID,
    QSAR_ANALYSIS_VIEW_ID,
)


# Narrower than this, an axis plus its tick labels leaves no room for the plot.
_MIN_READABLE_CHART_WIDTH = 240

# A chart needs more width than the 3D viewer whose slot it takes: two axes with their labels and
# tick numbers are overhead the plot itself doesn't get, so it opens a little wider than a third.
_CHART_WIDTH_SHARE = 0.40

# QMainWindow grants a dock resize geometrically rather than in one step (131 -> 516 -> 546 ->
# ... -> 576 for one claim of 576), so a claim takes several passes to land. Give it room to
# converge, then leave the layout alone.
_MAX_WIDTH_CLAIMS = 12


class _ChartWidthWatcher(QObject):
    """Every width the chart dock is given: the user's (kept) or the layout's (claimed back)."""

    def __init__(self, controller: "ChartController"):
        super().__init__(controller.w)
        self._controller = controller

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._controller._note_chart_width(event.size().width())
        return False


class ChartController:
    def __init__(self, window):
        self.w = window
        # The chart dock shares its slot with PyMOL (EXCLUSIVE region): every swap back hands the
        # chart whatever width the viewer left behind. Keep the chart's own width and restore it.
        self._chart_width = 0
        self._chosen = False  # the width came from a deliberate resize, not from our own share
        self._claims = 0  # claims made since the last show / user resize, see _MAX_WIDTH_CLAIMS
        self._widen_queued = False  # a claim is already on the event loop
        self._last_claim = -1  # the dock width the previous claim was issued against
        self._settling = False  # inside a slot swap: the widths it hands out are not anyone's choice
        self._width_watcher: _ChartWidthWatcher | None = None
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
        self.watch_chart_width()
        self.w.dock_manager.toggle(dock_id, True)
        if dock_id == "qsar_chart":
            # The chart takes PyMOL's slot, and with it PyMOL's width. toggle() lays the slot out
            # on the next event-loop pass, so claim the chart's own width after that.
            self._claims, self._last_claim = 0, -1  # a fresh swap; the budget bounds this one
            self._queue_widen(force=False)

    def _queue_widen(self, *, force: bool) -> None:
        """Claim the chart's width on the next pass. One claim at a time: several _show() calls
        can land in the same pass (each chart a panel draws reveals the dock), and queueing one
        timer apiece spent the whole claim budget before the layout had answered even once."""
        self._settling = True
        if self._widen_queued:
            return
        self._widen_queued = True

        def run():
            self._widen_queued = False
            self._widen_chart(force=force)

        QTimer.singleShot(0, run)

    def reveal(self) -> None:
        """Show the chart dock now, ahead of a chart still being computed, so its loading bar is
        seen where the chart will appear."""
        self._show()

    def size_chart_to_third(self) -> None:
        """The window reached its real (maximized) width: a chart already on screen - a project
        opened on QSAR Analysis - was laid out against the smaller window before it, so the share
        it was given is of the wrong total. Measure it again."""
        self.watch_chart_width()
        if self._chosen:
            # ...unless the width is the user's. This runs from resizeEvent, which fires on every
            # resize of a window that never maximizes, and re-measuring there would take the
            # divider they dragged away from them each time they touched the window edge.
            return
        self._claims, self._last_claim = 0, -1
        self._queue_widen(force=True)  # force: measure it against the window it now has

    def watch_chart_width(self) -> None:
        """Watch the chart dock's width from the moment it exists, whatever shows it later."""
        watcher = self._width_watcher
        if watcher is None and self._chart is not None:
            self._width_watcher = _ChartWidthWatcher(self)
            self._chart.installEventFilter(self._width_watcher)

    def _note_chart_width(self, width: int) -> None:
        """*When* the chart dock was resized decides what the width means. While a slot swap is
        settling, the width is the layout's doing - it hands the chart whatever PyMOL left behind -
        and is claimed back. Once the dock holds the width we claimed, the swap is over and every
        later resize was meant: a splitter drag, a window resize, a caller sizing the dock. Reading
        it off the resize keeps this independent of *which* code path showed the dock, and of how
        long a chart took to draw."""
        chart = self._chart
        if chart is None or chart.isFloating() or not chart.isVisible():
            return
        if not self._chart_width:
            # Nothing of ours to compare against yet, so there is no "it was left there": this is
            # simply the width the layout opened with. Take our share instead - reading it as a
            # preference would latch it, and _widen_chart would then never apply the share.
            self._queue_widen(force=True)
            return
        # A button down on the divider is unambiguous: the user is dragging it right now. That
        # outranks a swap still settling, which would otherwise claim the drag straight back.
        dragging = QApplication.mouseButtons() != Qt.MouseButton.NoButton
        if self._settling and not dragging:
            if width == self._chart_width or self._claims >= _MAX_WIDTH_CLAIMS:
                self._settling = False  # granted, or it never will be: stop overruling the layout
            else:
                self._queue_widen(force=False)
            return
        if width == self._chart_width:
            return  # a height-only resize, or the width we already hold: nothing was decided
        if width >= _MIN_READABLE_CHART_WIDTH:
            self._chart_width = width  # whoever set it, stop fighting it
            if dragging:
                # Theirs, so the startup re-measure must not take it back. A squeeze with no
                # button down - a switch to a tab whose centre needs more room - is taken as the
                # dock's width too, but is not marked as chosen, so the next re-measure can undo
                # it. The chart does not reclaim the share by itself when that tab is left.
                self._chosen = True
                self._settling = False
        self._claims = 0

    def _widen_chart(self, *, force: bool) -> None:
        """The chart's own width: the one the user left it at, else ``_CHART_WIDTH_SHARE`` of the
        window. Without this it inherits PyMOL's width every time it takes the shared slot back."""
        chart = self._chart
        if chart is None or not chart.isVisible() or chart.isFloating():
            # No width to claim on a dock nobody can see. Leave settling here, or the next width
            # the user picks would be read as the layout's and claimed straight back.
            self._settling = False
            return
        if not self._chart_width or force:
            self._chart_width = max(_MIN_READABLE_CHART_WIDTH,
                                    int(self.w.width() * _CHART_WIDTH_SHARE))
            self._chosen = False
        self._claims += 1
        if chart.width() == self._chart_width:
            # Already the width we claim, so nothing was resized and no resize event is coming to
            # end the settling. End it here, or every later resize would be overruled as the
            # layout's and a width chosen afterwards could never be kept.
            self._settling = False
            return
        # The layout will not always grant it - the centre may be at its minimum. Stop asking, and
        # above all stop settling: a dock left short otherwise stays locked, with every width the
        # user drags read as the layout's and taken straight back.
        if self._claims >= _MAX_WIDTH_CLAIMS or chart.width() == self._last_claim:
            self._settling = False  # out of tries, or the last claim moved nothing: refused
            return
        self._last_claim = chart.width()
        self.w.resizeDocks([chart], [self._chart_width], Qt.Horizontal)
        # Ask again next pass whether or not that landed. QMainWindow grants a dock resize
        # geometrically, and a refused one changes nothing at all, so waiting on a resize event
        # to drive the next claim would wait forever.
        self._queue_widen(force=False)

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
        if view_id == CLUSTERS_VIEW_ID:
            widget = self.w.central_widget.open_view(view_id)
            if widget is not None and hasattr(widget, "restore_plot"):
                widget.restore_plot()  # re-pushes the run's universe + toggles chart on
            return
        if view_id == QSAR_ANALYSIS_VIEW_ID:
            widget = self.w.central_widget.open_view(view_id)
            # only Model analysis charts; Result analysis keeps PyMOL (the else branch below)
            if widget is not None and widget.currentWidget() is widget.models:
                self._show()  # the chart takes PyMOL's place; the glow shows when a molecule is picked
                widget.restore_plot()
                return
        self.w.dock_manager.toggle("qsar_chart", False)
        if getattr(self.w, "qsar_glow_dock", None) is not None:
            self.w.dock_manager.toggle("qsar_glow", False)
        if self.w.pymol_dock is not None:
            self.w.dock_manager.toggle("pymol", True)
        # Big-data hygiene: the universe scatter can be heavy, so free it whenever we leave the
        # Clusters view. Its (small) point data stays in the widget → restore_plot redraws on return.
        self._chart.show_universe([], [], [0.0, 0.0])
        self._chart.hide_hover_structure()

    def show_activity_histogram(self, endpoint: str, bins) -> None:
        """bins = (labels, counts), already computed off the GUI thread by the caller."""
        if self._chart is None:
            return
        # The Activity view fills in the background - at start-up it is open behind QSAR Analysis -
        # and the chart dock is shared, so pushing from there would plant a distribution over the
        # model's own chart. It gets the chart when it is the view being read; coming back to it
        # re-pushes through sync_to_active_view.
        if self.w.central_widget.current_view_id() != LIGAND_ACTIVITY_VIEW_ID:
            return
        from amdockvs.ui.tools.qsar.chart import ClassCounts

        labels, counts = bins
        self._chart.show_distribution_bins(endpoint or "value", labels, counts,
                                           annotate=isinstance(bins, ClassCounts))
        self._show()

    def show_feature_importance(self, label: str, pairs, notes=None, images=None) -> None:
        """pairs = [(feature, importance), ...] (already sorted). Horizontal bars in PyMOL's region;
        ``notes`` = text on each bar's row, ``images`` = its hover PNG (b"" for none)."""
        if self._chart is None or not pairs:
            return
        pairs = list(pairs)[:15]  # already biggest-first; show_hbars renders index 0 at the top
        self._chart.show_hbars(
            f"Feature importance ({label})" if label else "Feature importance",
            [p[0] for p in pairs], [p[1] for p in pairs], "Importance", notes=notes, images=images,
        )
        self._show()

    def show_roc_curves(self, curves, empty: str = "No ROC curve.", subset: str = "") -> None:
        """curves = [{'label', 'points':[(fpr,tpr)], 'auc'}]. Overlaid ROC series + chance diagonal;
        none = an empty chart titled ``empty`` (a stale one would pass for this selection's)."""
        if self._chart is None:
            return
        sizes = {c.get("n") for c in curves}
        shared = f", n={next(iter(sizes)):,}" if len(sizes) == 1 and None not in sizes else ""
        title = f"ROC{f' · {subset}' if subset else ''}{shared}" if curves else empty  # model + AUC: legend
        self._chart.show_curves(title, curves, "False positive rate", "True positive rate", diagonal=True)
        self._show()

    def show_model_fit_series(self, series, empty: str = "No predictions to plot.") -> None:
        """series = [{'label','points':[(observed,predicted)]}]. Overlaid model-fit scatter; none =
        an empty chart titled ``empty``."""
        if self._chart is None:
            return
        title = (empty if not series else "Observed vs predicted" if len(series) > 1
                 else f"Observed vs predicted - {series[0].get('label', '')}")
        self._chart.show_scatter_series(title, series)
        self._show()

    def highlight_fit_molecule(self, molecule_id: int) -> None:
        if self._chart is not None:
            self._chart.highlight_molecule(molecule_id)

    def show_correlation_heatmap(self, label: str, labels, matrix) -> None:
        """Descriptor correlation heatmap (pyqtgraph) in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_heatmap(label or "Descriptor correlation", labels, matrix)
        self._show()

    def show_split_distribution(self, label: str, categories, groups, x_title: str = "") -> None:
        """Train-vs-test label distribution grouped bars (split preview) in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_grouped_bars(label or "Train/test distribution", categories, groups, x_title)
        self._show()

    def show_similarity_distribution(self, label: str, bins, empty: str = "") -> None:
        """bins = (labels, counts) of applicability-domain similarities, binned off the GUI thread;
        none = an empty chart titled ``empty`` (a stale one would pass for this selection\'s)."""
        if self._chart is None:
            return
        labels, counts = bins
        if not counts and empty:
            self._chart.show_message(empty)
        else:
            self._chart.show_distribution_bins(label or "AD similarity", labels, counts)
        self._show()

    def show_size_distribution(self, label: str, labels, counts) -> None:
        """Cluster-size distribution histogram for the Diversity view, in PyMOL's region."""
        if self._chart is None:
            return
        self._chart.show_distribution_bins(label or "cluster size", labels, counts)
        self._show()

    def show_diversity_universe(
            self, pool_points, selection_groups, evr=(), highlight_points=None
    ) -> None:
        """Accumulated chemical-universe scatter for the Diversity view, drawn in PyMOL's region.
        Empty inputs draw a placeholder (tab entry with no preview yet / after a merge).
        ``highlight_points`` emphasises one cluster's centroid(s) and dims the rest."""
        if self._chart is None:
            return
        self._chart.show_universe(pool_points, selection_groups, evr, highlight_points)
        self._show()

    def hide_glowing_molecule(self) -> None:
        if getattr(self.w, "qsar_glow_dock", None) is not None:
            self.w.dock_manager.toggle("qsar_glow", False)

    def show_glowing_molecule(self, molblock: str, weights, caption: str = "") -> None:
        if getattr(self.w, "qsar_glow_dock", None) is None:
            return
        opening = not self.w.qsar_glow_dock.isVisible()
        self.w.qsar_glow_dock.show_glowing(molblock, weights, caption)
        self._show("qsar_glow")
        if opening:  # half the column, as 2D Interactions gets; a later resize by the user stays
            QTimer.singleShot(0, self.w._split_viewer_docks_evenly)

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
            molblock = self.w.runtime.diversity.molblock_for(mid)
            return render_structure_png(molblock, legend=f"#{mid}") if molblock else b""

        run_async(_work, self._show_hovered_structure, on_error=lambda _e: None)

    def _show_hovered_structure(self, png_bytes: bytes) -> None:
        if png_bytes:
            self._chart.show_hover_structure(png_bytes)
