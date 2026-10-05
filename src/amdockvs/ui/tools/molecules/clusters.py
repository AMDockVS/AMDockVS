"""Clusters — what a Diversity run produced, as a catalog: clusters on top, their ligands below.

A run is data once it exists, so it is read here and not inside the tool that made it (the same
split as Docking Studio / Docking Results). Nothing on this view changes the library by itself:
selecting, excluding and saving as a set are the three explicit things you can do with a run.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.ui.catalog.ligands import LigandWidget, _ligand_table_config
from amdockvs.ui.common.async_query import run_async
from ms_components.ms_table import FilterOperator, FilterSpec

CLUSTERS_VIEW_ID = "workspace.clusters"

_TITLE = "Clusters"
_REP_COLOR = (214, 40, 40)
# ponytail: one QTableWidget row per cluster, largest first. A diverse library is mostly
# singletons, so past this only the big ones are listed; a model-backed table if that hurts.
_MAX_CLUSTER_ROWS = 2000
# ponytail: a selection is an `id IN [...]` filter, and SQLite binds at most 32766 values in its
# stock build. Larger picks go through "Save as set" (a subquery) instead.
_MAX_SELECTION = 30000


def _members_config(runtime):
    config = _ligand_table_config(runtime=runtime)
    # is_ligand only: an excluded ligand still belongs to its cluster.
    config.default_filters = list(config.default_filters or [])[:1]
    config.toolbar_left = []
    config.empty_message = "No ligands here"
    config.empty_action = None
    return config


class _ClusterLigands(LigandWidget):
    """The ligands of what is picked above. It is not the Ligands catalog, so it does not mirror
    the shared selection — "Select" here is what makes one."""

    selection_role = None

    def _select_rows(self, objects) -> None:
        store = self._store()
        ids = [int(obj.id) for obj in objects or () if getattr(obj, "id", None)]
        if store is not None and ids:
            store.set("ligand", ids)


class ClustersWidget(QWidget):
    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._runs: list[dict[str, Any]] = []
        self._run_id: str | None = None  # the run on screen
        self._wanted_run: str | None = None  # a run asked for before the list arrived
        self._pool: list = []  # the loaded run's points, split once: redrawn on every pick
        self._reps: list[dict[str, Any]] = []
        self._evr: list[float] = [0.0, 0.0]
        self._stats: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Run", self))
        self.run_combo = QComboBox(self)
        self.run_combo.currentIndexChanged.connect(self._on_run_changed)
        bar.addWidget(self.run_combo, 1)
        self.select_button = QPushButton("Use as selection", self)
        self.select_button.setToolTip(
            "Make the ligands listed below (the run's representatives, or the picked cluster) the "
            "ligand selection: the Ligands table, Docking and Build then work on them."
        )
        self.select_button.clicked.connect(self._use_as_selection)
        self.exclude_button = QPushButton("Exclude non-representatives", self)
        self.exclude_button.setToolTip(
            "Mark every ligand of this run that is not a representative as excluded. They stay in "
            "the project; reversible from Filter's 'Excluded' scope."
        )
        self.exclude_button.clicked.connect(self._exclude)
        self.save_button = QPushButton("Save as set…", self)
        self.save_button.setToolTip("Save the run's representatives as a ligand set.")
        self.save_button.clicked.connect(self._save_set)
        self.sizes_button = QPushButton("Size distribution", self)
        self.sizes_button.setToolTip(
            "Histogram of ligands-per-cluster: how many clusters are singletons vs. large. Replaces "
            "the scatter in the Distribution dock; picking a row redraws it."
        )
        self.sizes_button.clicked.connect(self._show_sizes)
        self.delete_button = QPushButton("Delete run", self)
        self.delete_button.clicked.connect(self._delete)
        self._buttons = (self.select_button, self.exclude_button, self.save_button,
                         self.sizes_button, self.delete_button)
        for button in self._buttons:
            bar.addWidget(button)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Cluster", "Ligands"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_cluster_picked)
        layout.addWidget(self.table, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Parentless: the window's auxiliary zone hosts it (see aux_panel), this view only owns it.
        self.members = _ClusterLigands(runtime=runtime, config=_members_config(runtime), parent=None)
        self.refresh()

    def aux_panel(self) -> QWidget:
        """The ligands of the picked row, shown in the window's auxiliary zone under this table."""
        return self.members

    # --- runs -----------------------------------------------------------------------
    def show_run(self, run_id: str) -> None:
        self._wanted_run = str(run_id)
        self.refresh()

    def refresh(self) -> None:
        run_async(self.runtime.diversity.list_clustering_results, self._fill_runs,
                  on_error=self._on_error, busy=self)

    refresh_view = refresh

    def _fill_runs(self, runs: list[dict[str, Any]]) -> None:
        self._runs = list(runs)
        wanted, self._wanted_run = self._wanted_run or self._run_id, None
        self.run_combo.blockSignals(True)
        self.run_combo.clear()
        for run in self._runs:
            when = run.get("created_at")
            when = when.strftime("%m-%d %H:%M") if hasattr(when, "strftime") else str(when or "")[:16]
            self.run_combo.addItem(
                f"{when} · {run.get('method')} t={run.get('threshold')} · {run.get('scope_label')} · "
                f"{run.get('n_molecules', 0)} ligands → {run.get('n_clusters', 0)} clusters",
                run["run_id"],
            )
        self.run_combo.setCurrentIndex(max(0, self.run_combo.findData(wanted)))
        self.run_combo.blockSignals(False)
        for button in self._buttons:
            button.setEnabled(bool(self._runs))
        if not self._runs:
            self._run_id = None
            self._show_run({})
            self.status.setText("No clustering runs yet — run one from the Diversity tool.")
        elif self.run_combo.currentData() != self._run_id:
            self._on_run_changed()

    def _on_run_changed(self, *_args) -> None:
        run_id = self.run_combo.currentData()
        if run_id:
            run_async(lambda: self.runtime.diversity.load_clustering_result(run_id), self._show_run,
                      on_error=self._on_error, busy=self)

    def _show_run(self, data: dict[str, Any]) -> None:
        self._run_id = data.get("run_id")
        points = data.get("points") or []
        self._pool = [[p["x"], p["y"]] for p in points if not p["is_centroid"]]
        self._reps = [p for p in points if p["is_centroid"]]
        self._evr = list(data.get("evr") or [0.0, 0.0])
        self._stats = sorted(data.get("cluster_stats") or [], key=lambda c: -int(c.get("size") or 0))
        shown = self._stats[:_MAX_CLUSTER_ROWS]
        rows = [("Representatives (one per cluster)" if data.get("n_reps") == data.get("n_clusters")
                 else "Representatives", data.get("n_reps", 0), None)] if self._run_id else []
        rows += [(f"Cluster {int(c['cluster_id'])}", int(c.get("size") or 0), int(c["cluster_id"])) for c in shown]
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for row, (name, size, cluster_id) in enumerate(rows):
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, cluster_id)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(str(size)))
        self.table.blockSignals(False)
        if rows:
            self.table.selectRow(0)  # fires _on_cluster_picked: the representatives go below
        else:
            self._on_cluster_picked()
        if self._run_id:
            cut = f" (the {len(shown)} largest listed)" if len(shown) < len(self._stats) else ""
            hint = (
                "Every cluster is a single ligand: nothing is redundant at this threshold."
                if data.get("n_clusters") == data.get("n_molecules")
                else "Pick a row to list its ligands below."
            )
            self.status.setText(
                f"{data.get('n_molecules', 0)} ligands in {data.get('n_clusters', 0)} clusters{cut}, "
                f"{data.get('n_reps', 0)} representatives. {hint}"
            )

    # --- the picked row ---------------------------------------------------------------
    def _picked_cluster(self) -> int | None:
        """The cluster id of the picked row; None for the representatives row."""
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return item.data(Qt.UserRole) if item is not None else None

    def _on_cluster_picked(self) -> None:
        clause = (
            self.runtime.diversity.members_clause(self._run_id, self._picked_cluster())
            if self._run_id else None
        )
        self.members.set_base_clause("cluster", clause)
        # No run: an always-false field filter rather than the whole ligand catalog.
        self.members.set_base_filter("id", None if self._run_id else FilterSpec("id", FilterOperator.EQ, 0))
        self.restore_plot()

    def restore_plot(self) -> None:
        """Draw the run in the Distribution dock: every ligand in grey, the representatives on
        top, the picked cluster's emphasised. Called again when this tab is re-entered."""
        window = self.window()
        show = getattr(window, "show_diversity_universe", None)
        central = getattr(window, "central_widget", None)
        # Only as the tab on screen: a run loaded behind another view must not take the chart.
        if show is None or not self._run_id or central is None or central.current_view_id() != CLUSTERS_VIEW_ID:
            return
        picked = self._picked_cluster()
        groups = [{
            "points": [[p["x"], p["y"]] for p in self._reps],
            "ids": [int(p["molecule_id"]) for p in self._reps],
            "cluster_ids": [int(p["cluster_id"]) for p in self._reps],
            "color": _REP_COLOR,
            "label": f"representatives ({len(self._reps)})",
            "clusters": self._stats,
        }] if self._reps else []
        highlight = [[p["x"], p["y"]] for p in self._reps if int(p["cluster_id"]) == picked]
        show(self._pool, groups, self._evr, highlight if picked is not None and highlight else None)

    # --- what you can do with a run ---------------------------------------------------
    def _use_as_selection(self) -> None:
        run_id, picked = self._run_id, self._picked_cluster()
        if not run_id:
            return

        def ids() -> list[int]:
            return [r["molecule_id"] for r in self.runtime.diversity.get_run(run_id)
                    if (r["is_centroid"] if picked is None else r["cluster_id"] == picked)]

        run_async(ids, self._apply_selection, on_error=self._on_error, busy=self)

    def _apply_selection(self, ids: list[int]) -> None:
        store = getattr(self.window(), "selection", None)
        if store is None or not ids:
            return
        if len(ids) > _MAX_SELECTION:
            QMessageBox.information(
                self, _TITLE,
                f"{len(ids)} ligands is too many for a table selection (limit {_MAX_SELECTION}). "
                f"Use 'Save as set…' or 'Exclude non-representatives' instead.",
            )
            return
        store.set("ligand", ids)
        self.status.setText(
            f"{len(ids)} ligand(s) selected: the Ligands table, Docking and Build now work on them. "
            f"Clear the ID filter of the Ligands table to drop the selection."
        )

    def _exclude(self) -> None:
        run_id = self._run_id
        run = next((r for r in self._runs if r["run_id"] == run_id), None)
        if run is None:
            return
        count = int(run.get("n_molecules", 0)) - int(run.get("n_reps", 0))
        if QMessageBox.question(
            self, _TITLE,
            f"Exclude the {count} non-representative ligands of this run?\n\n"
            f"They stay in the project, marked excluded; reversible from Filter's 'Excluded' scope.",
        ) != QMessageBox.Yes:
            return

        def done(excluded: int) -> None:
            self.status.setText(f"Excluded {excluded} non-representative ligand(s).")
            self.members.refresh()

        run_async(lambda: self.runtime.diversity.exclude_non_representatives(run_id), done,
                  on_error=self._on_error, busy=self)

    def _save_set(self) -> None:
        run_id = self._run_id
        if not run_id:
            return
        name, ok = QInputDialog.getText(self, _TITLE, "Set name:", text=f"representatives_{run_id[:8]}")
        if not ok:
            return
        run_async(
            lambda: self.runtime.diversity.save_centroids_as_set(run_id, name=name.strip()),
            lambda _ref: self.status.setText(f"Representatives saved as set '{name.strip()}'."),
            on_error=self._on_error, busy=self,
        )

    def _show_sizes(self) -> None:
        from amdockvs.diversity.clustering import size_histogram

        show = getattr(self.window(), "show_size_distribution", None)
        if show is not None and self._stats:
            labels, counts = size_histogram([int(c.get("size") or 0) for c in self._stats])
            show(f"cluster size ({len(self._stats)} clusters)", labels, counts)

    def _delete(self) -> None:
        run_id = self._run_id
        if not run_id or QMessageBox.question(
            self, _TITLE, "Delete this run? Ligands are not touched, excluded ones stay excluded."
        ) != QMessageBox.Yes:
            return
        self.runtime.diversity.delete_clustering_result(run_id)
        self._run_id = None
        self.refresh()

    def _on_error(self, exc: Exception) -> None:
        QMessageBox.critical(self, _TITLE, str(exc))


def register_clusters_workspace(window) -> None:
    window.register_main_view(
        CLUSTERS_VIEW_ID,
        _TITLE,
        lambda: ClustersWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = ["CLUSTERS_VIEW_ID", "ClustersWidget", "register_clusters_workspace"]
