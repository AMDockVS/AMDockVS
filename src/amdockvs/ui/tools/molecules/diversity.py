"""Diversity selection view — reduce chemical redundancy by clustering + centroid picking.

Two actions, clearly split:

* **Exclude non-representatives** IS the run, and it ALWAYS goes to an mf clustering job (never
  inline). It sizes the scope, then a hybrid dialog suggests ``plan_cpus(n)`` CPUs (1 → single-tree
  BitBIRCH, >1 → bblean multiround) which the user can override; the job requests exactly that many
  CPUs (``cpu_required``) so mf schedules it. When ``job_finished`` fires the non-representatives are
  inactivated automatically (same ``excluded`` flag Filter uses; reversible). No size cap.
* **Preview sample** is the only inline path: it clusters a *fresh* random sample (RUN_SAMPLE_LIMIT)
  just to eyeball the clustering and tune method/threshold. Previews ACCUMULATE (first fixes a PCA
  basis, later ones project onto it) into one grey 'chemical universe' scatter; they change nothing.

The scatter lives in the shared **Distribution** dock; this tab keeps the tightness + results tables.
"""
from __future__ import annotations

import gc
import json
import random
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from amdockvs.models.molecules import MoleculeUsageClass
from amdockvs.selection.api import SelectionAPI
from amdockvs.ui.async_query import run_async

SELECTION_VIEW_ID = "moltools.diversity"

# Preview clusters a small random sample so it stays fast (clustering is O(n·k·nbits) and the PCA
# SVD is superlinear — 20k mols took minutes). Exclusion at full scale goes through the parallel mf
# job, not this preview; above this the scope is sampled and inline exclusion stays disabled.
RUN_SAMPLE_LIMIT = 1000

# Cap the accumulated pile: each preview keeps points + render state alive, so an unbounded stack
# balloons RAM (the whole point of #6/#4). Past this, Clear (or apply-to-DB) is required.
MAX_PREVIEWS = 10

# Distinct marker colours, cycled one per preview so successive selections stack up readably.
_PALETTE = [
    (214, 40, 40), (58, 134, 255), (46, 196, 128), (255, 176, 32),
    (168, 100, 253), (0, 187, 204), (240, 98, 146),
]

def _jsonable_to_tuple(value: Any) -> Any:
    """Recursively turn JSON lists back into tuples so a reloaded scope key compares equal to a
    freshly-built one (``findData`` / the basis-key check both rely on tuple identity)."""
    return tuple(_jsonable_to_tuple(v) for v in value) if isinstance(value, list) else value


_EXCLUDE_TIP = (
    "Run the selection: cluster the WHOLE current scope as an mf job (always — never inline; no size "
    "cap) and inactivate the non-representatives, keeping one diverse molecule per cluster active "
    "(excluded=True). You confirm the CPU count first (suggested from the scope size; 1 = serial, "
    ">1 = parallel). The exclusion applies automatically when the job finishes. Preview is only a "
    "tuning look. Reversible from Filter's 'Excluded' scope."
)


class DiversitySelectionWidget(QWidget):
    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._running = False
        self._last_result: dict[str, Any] | None = None  # latest preview batch (for exclusion)
        # A parallel (mf) clustering run in flight: {job_id, run_id, reason, scope_label, method,
        # threshold} — its non-reps are excluded when job_finished fires for job_id.
        self._pending_cluster_job: dict[str, Any] | None = None
        self._job_signal_connected = False
        self._reset_accumulation()

        outer = QVBoxLayout(self)
        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to run diversity selection.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        outer.addWidget(self._build_controls())
        self.stats_label = QLabel(
            "Pick a scope, then 'Exclude non-representatives' clusters the whole selection and keeps one "
            "diverse molecule per cluster. 'Preview sample' is an optional quick look to tune the "
            "method/threshold first.",
            self,
        )
        self.stats_label.setWordWrap(True)
        outer.addWidget(self.stats_label)

        # view-mode state (a saved result on screen stashes the live pile)
        self._viewing: str | None = None
        self._live_snapshot: tuple | None = None
        self._results_display: list[dict[str, Any]] = []

        # left: saved results (durable, click to view) · right: clusters of the current preview/result
        self.results_table = QTableWidget(0, 3, self)
        self.results_table.setHorizontalHeaderLabels(["Saved", "Method", "n→clusters"])
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.results_table.setSelectionMode(QTableWidget.SingleSelection)
        self.results_table.itemSelectionChanged.connect(self._on_result_selected)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Clusters from"))
        self.preview_filter = QComboBox(self)
        self.preview_filter.addItem("All previews", None)
        self.preview_filter.currentIndexChanged.connect(self._refill_table)
        filter_row.addWidget(self.preview_filter)
        filter_row.addStretch(1)

        self.cluster_table = QTableWidget(0, 3, self)
        self.cluster_table.setHorizontalHeaderLabels(["Cluster", "Size", "Tightness"])
        self.cluster_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cluster_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cluster_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cluster_table.setSelectionMode(QTableWidget.SingleSelection)
        self.cluster_table.itemSelectionChanged.connect(self._on_cluster_selected)

        tables = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Results"))
        left.addWidget(self.results_table)
        right = QVBoxLayout()
        right.addLayout(filter_row)
        right.addWidget(self.cluster_table)
        tables.addLayout(left, 2)
        tables.addLayout(right, 3)
        outer.addLayout(tables, 1)

        self.refresh()
        self._refresh_results()
        self._load_cache()  # restore a pile left from a previous visit (the plot redraws on tab-enter)

    def _reset_accumulation(self) -> None:
        """Clear the fixed PCA basis + everything drawn/seen so the next preview starts a new pile."""
        self._basis = None
        self._basis_key: tuple | None = None
        self._seen_ids: set[int] = set()
        self._pool_points: list[list[float]] = []
        self._selections: list[dict[str, Any]] = []
        self._evr: list[float] = [0.0, 0.0]
        self._last_result = None
        self._total_in_scope = 0
        # Full per-point graph data for the whole pile (x, y, molecule_id, cluster_id, is_centroid) —
        # the parquet sidecar payload; also what Exclude/Save operate on across accumulated previews.
        self._all_points: list[tuple[float, float, int, int, bool]] = []
        self._highlight_points: list | None = None  # a picked cluster's centroid(s), emphasised on the plot

    # --- disk cache (previews survive tab close until Clear / applied) ---------
    def _cache_path(self) -> Path | None:
        try:
            project_root = self.runtime.get_project_paths()["project_root"]
        except Exception:  # no active project yet
            return None
        return Path(project_root) / "diversity_preview_cache.json"

    def _save_cache(self) -> None:
        """Persist the accumulated pile next to the project DB. Basis arrays are stored as plain
        lists (project_2d_onto asarray's them back). Best-effort — a failed write is non-fatal."""
        import numpy as np

        path = self._cache_path()
        if path is None:
            return
        basis = None
        if self._basis is not None:
            basis = {
                "mean": np.asarray(self._basis["mean"]).tolist(),
                "components": np.asarray(self._basis["components"]).tolist(),
                "evr": list(self._basis.get("evr") or [0.0, 0.0]),
            }
        payload = {
            "selections": self._selections, "pool_points": self._pool_points, "basis": basis,
            "basis_key": self._basis_key, "seen_ids": sorted(self._seen_ids), "evr": self._evr,
            "all_points": self._all_points, "total_in_scope": self._total_in_scope,
        }
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _load_cache(self) -> None:
        """Restore a saved pile on tab (re)open, and set the scope/FP controls back to what produced
        it so the next Preview extends it instead of resetting."""
        path = self._cache_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._selections = payload.get("selections") or []
        self._pool_points = payload.get("pool_points") or []
        self._basis = payload.get("basis")  # lists; project_2d_onto asarray's them
        self._basis_key = _jsonable_to_tuple(payload.get("basis_key"))
        self._seen_ids = {int(i) for i in (payload.get("seen_ids") or [])}
        self._evr = payload.get("evr") or [0.0, 0.0]
        self._all_points = [tuple(p) for p in (payload.get("all_points") or [])]
        self._total_in_scope = int(payload.get("total_in_scope") or 0)
        self._last_result = None  # method/threshold fall back to the current controls on a restored pile
        if self._basis_key:
            scope_data, radius, nbits = self._basis_key
            # QComboBox.findData compares via QVariant and misses nested-tuple data — search by ==.
            idx = next((i for i in range(self.set_combo.count()) if self.set_combo.itemData(i) == scope_data), -1)
            for widget, setter, value in (
                (self.set_combo, self.set_combo.setCurrentIndex, idx),
                (self.radius_spin, self.radius_spin.setValue, int(radius)),
                (self.nbits_spin, self.nbits_spin.setValue, int(nbits)),
            ):
                if value is not None and value != -1:
                    widget.blockSignals(True)
                    setter(value)
                    widget.blockSignals(False)
        self._sync_preview_filter()
        self._refill_table()
        if self._selections:
            self.stats_label.setText(
                f"Restored {len(self._selections)} cached preview sample(s) · {len(self._seen_ids)} "
                f"molecules. Press 'Exclude non-representatives' to run over the whole selection."
            )

    def _delete_cache(self) -> None:
        path = self._cache_path()
        if path is not None:
            path.unlink(missing_ok=True)

    # --- controls -------------------------------------------------------------
    def _build_controls(self) -> QWidget:
        box = QGroupBox("Diversity selection", self)
        row = QHBoxLayout(box)

        # left: shared setup (what to cluster, feature, actions) --------------------------------
        setup = QFormLayout()
        self.set_combo = QComboBox(box)  # Selection scope (usage classes + sets)
        self.set_combo.setToolTip("What to cluster — clustering is often aimed at a subset, not the whole library.")
        self.method_combo = QComboBox(box)
        for name in SelectionAPI.supported_methods():
            self.method_combo.addItem(name, name)

        self.per_cluster_spin = QSpinBox(box)
        self.per_cluster_spin.setRange(1, 20)
        self.per_cluster_spin.setValue(1)
        self.per_cluster_spin.setToolTip("Representatives kept per cluster (the members closest to the centroid).")

        self.radius_spin = QSpinBox(box)
        self.radius_spin.setRange(1, 4)
        self.radius_spin.setValue(2)
        self.nbits_spin = QSpinBox(box)
        self.nbits_spin.setRange(64, 8192)
        self.nbits_spin.setSingleStep(512)
        self.nbits_spin.setValue(2048)
        fp_row = QHBoxLayout()
        fp_row.addWidget(QLabel("radius"))
        fp_row.addWidget(self.radius_spin)
        fp_row.addWidget(QLabel("nbits"))
        fp_row.addWidget(self.nbits_spin)
        fp_row.addStretch(1)

        setup.addRow("Selection", self.set_combo)
        setup.addRow("Method", self.method_combo)
        setup.addRow("Per cluster", self.per_cluster_spin)
        setup.addRow("Morgan FP", fp_row)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Preview sample", box)
        self.run_button.setToolTip(
            f"Optional: cluster a fast random sample (up to {RUN_SAMPLE_LIMIT}) just to eyeball the "
            f"clustering and tune threshold/method — changes nothing. Press it again for another "
            f"sample. To actually run the selection, use 'Exclude non-representatives'."
        )
        self.run_button.clicked.connect(self._run)
        self.clear_button = QPushButton("Clear", box)
        self.clear_button.setToolTip("Forget the accumulated preview samples and start a fresh pile.")
        self.clear_button.clicked.connect(self._clear)
        self.size_dist_button = QPushButton("Size distribution", box)
        self.size_dist_button.setToolTip(
            "Histogram of compounds-per-cluster for the current previews / viewed result — how many "
            "clusters are singletons vs. large. Replaces the scatter in the Distribution dock; Preview redraws it."
        )
        self.size_dist_button.clicked.connect(self._show_size_distribution)
        self.exclude_button = QPushButton("Exclude non-representatives", box)
        self.exclude_button.setToolTip(_EXCLUDE_TIP)
        self.exclude_button.clicked.connect(self._exclude_nonreps)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.size_dist_button)
        buttons.addWidget(self.exclude_button)
        buttons.addStretch(1)
        setup.addRow(buttons)

        # right: the selected method's own options (a page per method) --------------------------
        self.method_stack = QStackedWidget(box)
        self._build_method_pages()
        method_box = QGroupBox("Method options", box)
        method_layout = QVBoxLayout(method_box)
        method_layout.addWidget(self.method_stack)
        method_layout.addStretch(1)

        # changing the method swaps the options page (and invalidates nothing — Preview re-reads them)
        self.method_combo.currentIndexChanged.connect(self._on_method_changed)
        self._on_method_changed()

        row.addLayout(setup, 3)
        row.addWidget(method_box, 2)
        return box

    def _build_method_pages(self) -> None:
        """One options page per registered method (QStackedWidget). Each page owns that method's
        knobs; ``_cluster_params`` reads the active page. New methods add a page the same way they
        register in ``CLUSTERING_METHODS``."""
        self._method_getters: dict[str, dict[str, Any]] = {}
        self._method_page_index: dict[str, int] = {}
        for name in SelectionAPI.supported_methods():
            page = QWidget()
            form = QFormLayout(page)
            getters: dict[str, Any] = {}

            threshold = QDoubleSpinBox(page)
            threshold.setRange(0.0, 1.0)
            threshold.setSingleStep(0.05)
            threshold.setDecimals(2)
            threshold.setValue(0.35)
            threshold.setToolTip(
                "Merge threshold. Higher = tighter, more clusters, less reduction. Range is "
                "fingerprint-dependent: 0.3–0.4 for ECFP4 (Morgan r2, the default here), 0.5–0.65 for "
                "RDKit fingerprints (bblean guidance)."
            )
            form.addRow("Threshold", threshold)
            getters["threshold"] = lambda w=threshold: float(w.value())

            if name == "bitbirch_lean_parallel":
                batch = QSpinBox(page)
                batch.setRange(100, 100000)
                batch.setSingleStep(500)
                batch.setValue(2000)
                batch.setToolTip("Molecules per batch clustered independently — the parallelizable unit.")
                form.addRow("Batch size", batch)
                getters["batch_size"] = lambda w=batch: int(w.value())

            self._method_page_index[name] = self.method_stack.addWidget(page)
            self._method_getters[name] = getters

    def _on_method_changed(self) -> None:
        name = str(self.method_combo.currentData() or "")
        self.method_stack.setCurrentIndex(self._method_page_index.get(name, 0))

    # --- scope helpers --------------------------------------------------------
    def refresh(self) -> None:
        """Repopulate the Selection scope: usage classes (like Filter) + saved sets. currentData is a
        (kind, value) tuple → ('all', None) | ('usage', class|classes) | ('set', id)."""
        selected = self.set_combo.currentData()
        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        self.set_combo.addItem("All ligands", ("all", None))
        self.set_combo.addItem("General", ("usage", MoleculeUsageClass.GENERAL))
        self.set_combo.addItem("Reference", ("usage", MoleculeUsageClass.REFERENCE))
        self.set_combo.addItem(
            "General + Reference", ("usage", (MoleculeUsageClass.GENERAL, MoleculeUsageClass.REFERENCE))
        )
        for record in self.runtime.molecules.list_sets():
            self.set_combo.addItem(
                f"Set #{int(record.id or 0)}  {record.name or 'unnamed_set'}", ("set", int(record.id or 0))
            )
        index = self.set_combo.findData(selected)
        self.set_combo.setCurrentIndex(index if index >= 0 else 0)
        self.set_combo.blockSignals(False)

    def _scope_params(self) -> dict[str, Any]:
        kind, value = self.set_combo.currentData() or ("all", None)
        molecule_filters: dict[str, Any] = {}
        if kind == "usage":
            molecule_filters["usage_class__in" if isinstance(value, tuple) else "usage_class"] = (
                list(value) if isinstance(value, tuple) else value
            )
        return {
            "molecule_set": self.runtime.molecules.resolve_set(value) if kind == "set" else None,
            "molecule_filters": molecule_filters,
            "fp_radius": int(self.radius_spin.value()),
            "fp_nbits": int(self.nbits_spin.value()),
            "sample_limit": RUN_SAMPLE_LIMIT,
        }

    def _scope_key(self) -> tuple:
        return (self.set_combo.currentData(), int(self.radius_spin.value()), int(self.nbits_spin.value()))

    def _cluster_params(self) -> dict[str, Any]:
        method = str(self.method_combo.currentData() or "bitbirch")
        params: dict[str, Any] = {"method": method, "per_cluster": int(self.per_cluster_spin.value())}
        for key, getter in self._method_getters.get(method, {}).items():
            params[key] = getter()
        return params

    # --- preview (manual, off the GUI thread, with a blocking overlay) --------
    def _clear(self) -> None:
        self._exit_view()
        self._reset_accumulation()
        self.cluster_table.setRowCount(0)
        self._sync_preview_filter()
        self.exclude_button.setEnabled(True)
        self.stats_label.setText(
            "Cleared. 'Exclude non-representatives' runs the clustering over the whole selection; "
            "'Preview sample' is an optional look first."
        )
        self._push_plot()
        self._delete_cache()
        gc.collect()  # drop the freed scatter/data promptly (RSS may still lag — allocator-held)

    def _run(self) -> None:
        if self._running:
            return
        self._exit_view()  # leave a viewed result → back to the live pile
        key = self._scope_key()
        if key != self._basis_key:  # new scope/FP → the fixed PCA basis no longer applies
            self._reset_accumulation()
            self._basis_key = key
        if len(self._selections) >= MAX_PREVIEWS:
            QMessageBox.information(
                self, "Diversity selection",
                f"Preview limit ({MAX_PREVIEWS}) reached — press Clear to start a fresh pile, or "
                f"exclude/apply to finish.",
            )
            return
        self._running = True
        self.run_button.setEnabled(False)
        self.exclude_button.setEnabled(False)
        scope_kw = self._scope_params()
        cluster_kw = self._cluster_params()
        seed = random.randrange(1 << 30)  # a fresh subset every press
        run_async(
            lambda: self._load_and_cluster(scope_kw, cluster_kw, seed),
            self._on_previewed,
            on_error=self._on_error,
            busy=self,
        )

    def _load_and_cluster(self, scope_kw: dict, cluster_kw: dict, seed: int) -> dict[str, Any]:
        universe = self.runtime.selection.load_universe(
            seed=seed, basis=self._basis, exclude_ids=self._seen_ids, **scope_kw
        )
        analysis = self.runtime.selection.cluster_loaded(universe, **cluster_kw)
        return {"basis": universe.basis, "analysis": analysis.to_mapping()}

    def _on_previewed(self, payload: dict[str, Any]) -> None:
        self._running = False
        self.run_button.setEnabled(True)
        self.exclude_button.setEnabled(True)
        result = payload["analysis"]
        ids = result.get("molecule_ids") or []
        if not ids:
            if not self._pool_points:
                self.stats_label.setText(
                    "No fingerprintable molecules found in this scope. Import ligands (or compute "
                    "fingerprints) first."
                )
            else:
                self.stats_label.setText(
                    f"Whole scope already previewed ({len(self._seen_ids)} molecules across "
                    f"{len(self._selections)} previews)."
                )
            return
        if self._basis is None:  # first preview of this scope fixes the shared PCA axes
            self._basis = payload["basis"]
            self._evr = list(result.get("projection_variance") or [0.0, 0.0])

        stats = result.get("stats") or {}
        reps = set(result.get("representative_ids") or [])
        projection = result.get("projection") or []
        labels = result.get("labels") or []
        self._total_in_scope = int(result.get("total_in_scope") or 0)
        self._seen_ids.update(int(m) for m in ids)
        preview_no = len(self._selections) + 1  # this batch's number (group appended just below)
        rep_pts: list[Any] = []
        rep_ids: list[int] = []
        rep_cluster_ids: list[int] = []
        for i, m in enumerate(ids):
            x, y = projection[i]
            is_centroid = m in reps
            raw_label = int(labels[i]) if i < len(labels) else 0
            # ponytail: cluster ids are namespaced per preview (×1e6) so labels from independent
            # batches never collide in the saved graph — bumps if a batch exceeds 1e6 clusters.
            cluster_id = preview_no * 1_000_000 + raw_label
            self._all_points.append((float(x), float(y), int(m), cluster_id, is_centroid))
            if is_centroid:
                rep_pts.append(projection[i])
                rep_ids.append(int(m))
                rep_cluster_ids.append(raw_label)  # raw label matches the cluster table's id
            else:
                self._pool_points.append(projection[i])
        color = _PALETTE[len(self._selections) % len(_PALETTE)]
        self._selections.append({
            "points": rep_pts,
            "ids": rep_ids,  # parallel to points → hover maps a marker back to its centroid molecule
            "cluster_ids": rep_cluster_ids,  # parallel to points → click-a-cluster highlight
            "color": color,
            "label": f"preview {len(self._selections) + 1} ({len(rep_pts)})",
            "clusters": stats.get("clusters") or [],
        })
        self._last_result = result

        self.stats_label.setText(
            f"Preview sample {len(self._selections)}: {stats.get('n_molecules', 0)} molecules → "
            f"{stats.get('n_clusters', 0)} clusters ({len(reps)} representatives) · "
            f"mean tightness {stats.get('mean_tightness', 0)}.\n"
            f"Sampled {len(self._seen_ids)} of {self._total_in_scope} in scope across "
            f"{len(self._selections)} preview(s) — this is just a look. Press "
            f"'Exclude non-representatives' to run the clustering over the whole selection."
        )
        self._sync_preview_filter()
        self._refill_table()
        self._push_plot()
        self._save_cache()

    def _sync_preview_filter(self) -> None:
        """Keep the 'Clusters from' combo in sync with the accumulated previews (blocking its signal
        so repopulating doesn't refill the table twice)."""
        current = self.preview_filter.currentData()
        self.preview_filter.blockSignals(True)
        self.preview_filter.clear()
        self.preview_filter.addItem("All previews", None)
        for i in range(1, len(self._selections) + 1):
            self.preview_filter.addItem(f"preview {i}", i)
        index = self.preview_filter.findData(current)
        self.preview_filter.setCurrentIndex(index if index >= 0 else 0)
        self.preview_filter.blockSignals(False)

    def _refill_table(self) -> None:
        """Fill the cluster table from the selected preview (or all). Cluster names are `r{n}_{id}` when
        viewing a saved result, `p{n}_{id}` for live previews — so the two never get confused. The
        (group_index, cluster_id) is stashed on each row for the click-to-highlight."""
        prefix = "r" if self._viewing is not None else "p"
        chosen = self.preview_filter.currentData()  # None = all previews
        rows: list[tuple[str, Any, Any, int, int, int]] = []
        for preview_no, group in enumerate(self._selections, start=1):
            if chosen is not None and chosen != preview_no:
                continue
            for cluster in group.get("clusters") or []:
                cluster_id = int(cluster.get("cluster_id") or 0)
                rows.append((
                    f"{prefix}{preview_no}_{cluster_id}",
                    cluster.get("size"), cluster.get("tightness"), int(cluster.get("size") or 0),
                    preview_no - 1, cluster_id,
                ))
        rows.sort(key=lambda r: r[3], reverse=True)
        self.cluster_table.blockSignals(True)
        self.cluster_table.setRowCount(len(rows))
        for row, (name, size, tightness, _, group_index, cluster_id) in enumerate(rows):
            name_item = QTableWidgetItem(str(name))
            name_item.setData(Qt.UserRole, (group_index, cluster_id))  # for _on_cluster_selected
            self.cluster_table.setItem(row, 0, name_item)
            self.cluster_table.setItem(row, 1, QTableWidgetItem(str(size)))
            self.cluster_table.setItem(row, 2, QTableWidgetItem(str(tightness)))
        self.cluster_table.blockSignals(False)

    def _on_cluster_selected(self) -> None:
        """Click a cluster row → emphasise that cluster's centroid(s) on the plot and dim the rest.
        Deselecting (clicking empty space) or re-viewing the result clears it."""
        items = self.cluster_table.selectedItems()
        data = self.cluster_table.item(items[0].row(), 0).data(Qt.UserRole) if items else None
        if not data:
            self._highlight_points = None
            self._push_plot()
            return
        group_index, cluster_id = data
        group = self._selections[group_index] if 0 <= group_index < len(self._selections) else None
        pts: list = []
        if group is not None:
            cluster_ids = group.get("cluster_ids") or []
            group_points = group.get("points") or []
            pts = [group_points[i] for i, cid in enumerate(cluster_ids)
                   if cid == cluster_id and i < len(group_points)]
        self._highlight_points = pts or None
        self._push_plot()

    # --- plot lives in the shared Distribution dock ---------------------------
    def _push_plot(self) -> None:
        show = getattr(self.window(), "show_diversity_universe", None)
        if show is not None:
            show(self._pool_points, self._selections, self._evr, self._highlight_points)

    def restore_plot(self) -> None:
        """Re-push the accumulated universe (or a placeholder) when the tab is (re-)entered."""
        self._push_plot()

    def _show_size_distribution(self) -> None:
        """Draw the compounds-per-cluster histogram over the current previews / viewed result."""
        from amdockvs.selection.clustering import size_histogram

        sizes = [int(c.get("size") or 0)
                 for group in self._selections for c in (group.get("clusters") or [])]
        if not sizes:
            QMessageBox.information(
                self, "Diversity selection",
                "No clusters yet — Preview a sample or open a saved result first.",
            )
            return
        labels, counts = size_histogram(sizes)
        show = getattr(self.window(), "show_size_distribution", None)
        if show is not None:
            show(f"cluster size ({len(sizes)} clusters)", labels, counts)

    # --- saved results (durable, DB summary + parquet sidecar) ----------------
    def _refresh_results(self) -> None:
        try:
            self._results_display = self.runtime.selection.list_clustering_results()
        except Exception:  # noqa: BLE001 — an empty/absent table just means no runs yet
            self._results_display = []
        self.results_table.blockSignals(True)
        self.results_table.setRowCount(len(self._results_display))
        for row, res in enumerate(self._results_display):
            when = res.get("created_at")
            when_text = when.strftime("%m-%d %H:%M") if hasattr(when, "strftime") else str(when or "")[:16]
            self.results_table.setItem(row, 0, QTableWidgetItem(when_text))
            self.results_table.setItem(row, 1, QTableWidgetItem(str(res.get("method", ""))))
            self.results_table.setItem(row, 2, QTableWidgetItem(f"{res.get('n_molecules', 0)}→{res.get('n_clusters', 0)}"))
        self.results_table.clearSelection()
        self.results_table.blockSignals(False)

    def _on_result_selected(self) -> None:
        model = self.results_table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return
        idx = rows[0].row()
        if not (0 <= idx < len(self._results_display)):
            return
        run_id = self._results_display[idx]["run_id"]
        run_async(
            lambda: self.runtime.selection.load_clustering_result(run_id),
            self._view_result, on_error=self._on_error, busy=self,
        )

    def _view_result(self, data: dict[str, Any]) -> None:
        """Show a saved run read-only: its graph + clusters, drawn straight from the sidecar."""
        if self._viewing is None:  # stash the live pile so Preview can return to it
            self._live_snapshot = (
                self._selections, self._pool_points, self._basis, self._basis_key,
                self._seen_ids, self._evr, self._last_result, self._all_points, self._total_in_scope,
            )
        self._viewing = data.get("run_id")
        points = data.get("points") or []
        reps = [p for p in points if p.get("is_centroid")]
        self._pool_points = [[p["x"], p["y"]] for p in points if not p.get("is_centroid")]
        self._selections = [{
            "points": [[p["x"], p["y"]] for p in reps],
            "ids": [int(p["molecule_id"]) for p in reps],
            "cluster_ids": [int(p["cluster_id"]) for p in reps],  # parallel → click-a-cluster highlight
            "color": _PALETTE[0],
            "label": f"result ({len(reps)})",
            "clusters": data.get("cluster_stats") or [],
        }]
        self._evr = data.get("evr") or [0.0, 0.0]
        self._last_result = None
        self._highlight_points = None  # re-viewing resets any picked-cluster emphasis
        self.exclude_button.setEnabled(False)  # a saved run is read-only
        self.stats_label.setText(
            f"Viewing saved result · {data.get('method')} t={data.get('threshold')} · "
            f"{data.get('n_molecules', 0)}→{data.get('n_clusters', 0)} clusters, {data.get('n_reps', 0)} reps. "
            f"Press Preview to return to the live pile."
        )
        self._sync_preview_filter()
        self._refill_table()
        self._push_plot()

    def _exit_view(self) -> None:
        if self._viewing is not None and self._live_snapshot is not None:
            (self._selections, self._pool_points, self._basis, self._basis_key,
             self._seen_ids, self._evr, self._last_result, self._all_points,
             self._total_in_scope) = self._live_snapshot
        self._viewing = None
        self._live_snapshot = None
        self._highlight_points = None  # leaving the result clears any picked-cluster emphasis
        self.exclude_button.setEnabled(True)  # back on the live scope → the run is available again
        self.results_table.blockSignals(True)
        self.results_table.clearSelection()
        self.results_table.blockSignals(False)

    # --- the run: ALWAYS an mf job (serial=1 CPU or parallel multiround); exclude on completion ----
    def _exclude_nonreps(self) -> None:
        """Run the selection over the ENTIRE scope. This always goes to an mf clustering job (never
        inline — Preview is the only inline path). First size the scope off the GUI thread, then let
        the user confirm/override the CPU count and submit the job; the non-representatives are
        inactivated automatically when it finishes."""
        if self._running or self._pending_cluster_job is not None:
            return
        self._exit_view()  # leave a viewed result → operate on the live scope
        self._running = True
        self.run_button.setEnabled(False)
        self.exclude_button.setEnabled(False)
        scope_kw = self._scope_params()
        scope_kw.pop("sample_limit", None)  # the run clusters everything
        cluster_kw = self._cluster_params()
        self.stats_label.setText("Sizing the selection…")
        run_async(
            lambda: self.runtime.selection.scope_count(
                molecule_set=scope_kw["molecule_set"], molecule_filters=scope_kw["molecule_filters"],
                fp_radius=scope_kw["fp_radius"], fp_nbits=scope_kw["fp_nbits"],
            ),
            lambda n: self._confirm_and_submit(int(n), scope_kw, cluster_kw),
            on_error=self._on_error,
            busy=self,
        )

    def _confirm_and_submit(self, n: int, scope_kw: dict, cluster_kw: dict) -> None:
        """Hybrid CPU choice: suggest ``plan_cpus(n)`` (1 → serial, >1 → parallel multiround), let the
        user override up to the machine's cores, then submit the mf job requesting exactly that many."""
        import os

        from amdockvs.selection.api import plan_cpus

        self._running = False
        self.run_button.setEnabled(True)
        if n <= 0:
            self.exclude_button.setEnabled(True)
            self.stats_label.setText("No molecules in this scope.")
            return
        cap = os.cpu_count() or 1
        suggested = plan_cpus(n, cap=cap)
        cpus, ok = QInputDialog.getInt(
            self, "Run clustering",
            f"{n} molecules — clustering runs as an mf job.\n"
            f"{'Parallel' if suggested > 1 else 'Serial'} at the suggested {suggested} CPU(s); "
            f"override up to {cap} cores:",
            suggested, 1, cap, 1,
        )
        if not ok:
            self.exclude_button.setEnabled(True)
            self.stats_label.setText("Run cancelled.")
            return
        cpus = int(cpus)
        run_id = uuid.uuid4().hex
        method = str(cluster_kw.get("method") or "bitbirch")
        threshold = float(cluster_kw.get("threshold") or 0.35)
        try:
            job_id = self.runtime.selection.cluster_job(
                method=method, threshold=threshold, per_cluster=int(cluster_kw.get("per_cluster") or 1),
                molecule_set=scope_kw["molecule_set"], molecule_filters=scope_kw["molecule_filters"],
                fp_radius=scope_kw["fp_radius"], fp_nbits=scope_kw["fp_nbits"],
                cluster_run_id=run_id, num_cpus=cpus,
            )
        except Exception as exc:  # noqa: BLE001 — submission failure must not wedge the button
            self.exclude_button.setEnabled(True)
            QMessageBox.critical(self, "Diversity selection", f"Could not submit the clustering job: {exc}")
            return
        self._pending_cluster_job = {
            "job_id": str(job_id), "run_id": run_id, "method": method, "threshold": threshold,
            "scope_label": self.set_combo.currentText(),
            "fp_radius": int(scope_kw["fp_radius"]), "fp_nbits": int(scope_kw["fp_nbits"]),
            "reason": f"diversity: non-representative ({method}, t={threshold})",
        }
        self._connect_job_signal()
        self.exclude_button.setEnabled(False)  # one clustering run at a time
        mode = "parallel multiround" if cpus > 1 else "serial"
        self.stats_label.setText(
            f"Clustering {n} molecules as an mf job on {cpus} CPU(s) ({mode}) — see Jobs. The "
            f"non-representatives are inactivated automatically when it finishes; you can keep working."
        )

    def _connect_job_signal(self) -> None:
        if self._job_signal_connected:
            return
        bridge = getattr(self.window(), "monitor_bridge", None)
        if bridge is not None:
            bridge.job_finished.connect(self._on_cluster_job_finished)
            self._job_signal_connected = True

    def _on_cluster_job_finished(self, job_id: str, status: str) -> None:
        pending = self._pending_cluster_job
        if not pending or str(job_id) != pending["job_id"]:
            return
        if str(status or "").strip().lower() != "completed":  # failed / canceled → don't exclude
            self._pending_cluster_job = None
            self.exclude_button.setEnabled(True)
            self.stats_label.setText(f"Parallel clustering job {status} — nothing excluded. See Jobs.")
            return
        self._pending_cluster_job = None
        run_async(
            lambda p=pending: self._apply_parallel_run(p),
            self._on_parallel_applied,
            on_error=self._on_error,
            busy=self.stats_label,
            compact=True,
        )

    def _apply_parallel_run(self, pending: dict[str, Any]) -> dict[str, Any]:
        """Off-thread: read the finished run's assignments, inactivate the non-centroids, and register
        the saved result. The job already wrote the graph sidecar (PCA + clusters), so registering
        just reads it — nothing is recomputed."""
        run_id = pending["run_id"]
        rows = self.runtime.selection.get_run(run_id)
        non_reps = [int(r["molecule_id"]) for r in rows if not r["is_centroid"]]
        n_clusters = len({int(r["cluster_id"]) for r in rows})
        n_reps = sum(1 for r in rows if r["is_centroid"])
        count = (
            self.runtime.molecules.set_excluded_state(non_reps, excluded=True, reason=pending["reason"])
            if non_reps else 0
        )
        self.runtime.selection.register_run_from_sidecar(
            run_id, method=pending["method"], threshold=pending["threshold"],
            scope_label=pending["scope_label"],
            fp_radius=int(pending["fp_radius"]), fp_nbits=int(pending["fp_nbits"]),
        )
        return {"count": int(count), "n_clusters": n_clusters, "n_reps": n_reps,
                "threshold": pending["threshold"]}

    def _on_parallel_applied(self, summary: dict[str, Any]) -> None:
        self.exclude_button.setEnabled(True)
        self._refresh_results()
        n_clusters, count = summary["n_clusters"], summary["count"]
        threshold = summary.get("threshold")
        if count == 0:
            # Every cluster is a single molecule → nothing redundant at this threshold. This is the
            # expected outcome for an already-diverse library, not an error.
            msg = (
                f"Clustered into {n_clusters} clusters, but every one is a single molecule — nothing is "
                f"redundant at threshold {threshold}. This scope is already diverse (a diversity set has "
                f"little to reduce). Lower the threshold to force more merging, or accept it as-is."
            )
            self.stats_label.setText(msg)
            QMessageBox.information(self, "Diversity selection", msg)
            return
        self.stats_label.setText(
            f"Run done: {n_clusters} clusters, {summary['n_reps']} representatives · inactivated {count} "
            f"non-representatives. Saved as a result — click it to see its clusters and universe."
        )
        QMessageBox.information(
            self, "Diversity selection",
            f"Clustering finished — inactivated {count} non-representative molecules.",
        )

    def _on_error(self, exc: Exception) -> None:
        self._running = False
        self.run_button.setEnabled(True)
        self.exclude_button.setEnabled(self._viewing is None)
        QMessageBox.critical(self, "Diversity selection", str(exc))


def register_selection_workspace(window) -> None:
    window.register_main_view(
        SELECTION_VIEW_ID,
        "Diversity Selection",
        lambda: DiversitySelectionWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = ["SELECTION_VIEW_ID", "DiversitySelectionWidget", "register_selection_workspace"]
