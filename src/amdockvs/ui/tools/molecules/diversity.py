"""Diversity — cluster a ligand scope into groups of similar molecules, one representative each.

The scope is what the Ligands table shows: the Selection combo is pushed onto that table while
this tool is up, and whatever is narrowed there (a "Select", a column filter) narrows the run too.

* **Cluster** IS the run, and it ALWAYS goes to an mf clustering job (never inline). It sizes the
  scope, then a hybrid dialog suggests ``plan_cpus(n)`` CPUs (1 → single-tree BitBIRCH, >1 → bblean
  multiround) which the user can override; the job requests exactly that many CPUs
  (``cpu_required``) so mf schedules it. When ``job_finished`` fires the run is registered and
  opens in the **Clusters** view. It changes nothing in the library: selecting, excluding and
  saving as a set are explicit actions of that view. No size cap.
* **Preview sample** is the only inline path: it clusters a *fresh* random sample (RUN_SAMPLE_LIMIT)
  just to eyeball the clustering and tune method/threshold. Previews ACCUMULATE (first fixes a PCA
  basis, later ones project onto it) into one grey 'chemical universe' scatter in the shared
  **Distribution** dock.
"""
from __future__ import annotations

import gc
import json
import random
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from amdockvs.models.molecules import MoleculeUsageClass
from amdockvs.diversity.api import DiversityAPI
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.tools.molecules.clusters import CLUSTERS_VIEW_ID
from ms_components.ms_table import FilterOperator, FilterSpec

SELECTION_VIEW_ID = "moltools.diversity"

# Preview clusters a small random sample so it stays fast (clustering is O(n·k·nbits) and the PCA
# SVD is superlinear — 20k mols took minutes). The run at full scale goes through the mf job, not
# this preview; above this the scope is sampled.
RUN_SAMPLE_LIMIT = 1000

# ponytail: a narrowed Ligands table reaches the run as an `id IN [...]` list, and SQLite binds at
# most 32766 values in its stock build. Past this, save the rows as a set and pick it in Selection;
# the scale-free version is scope_ids() as a subquery (see BoundTableWidget.scope_ids).
MAX_NARROWED_IDS = 30000

# Cap the accumulated pile: each preview keeps points + render state alive, so an unbounded stack
# balloons RAM (the whole point of #6/#4). Past this, Clear is required.
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


_CLUSTER_TIP = (
    "Cluster the WHOLE scope as an mf job (always — never inline; no size cap). You confirm the CPU "
    "count first (suggested from the scope size; 1 = serial, >1 = parallel). The result opens in "
    "the Clusters view when the job finishes; nothing is excluded until you ask for it there."
)


class DiversitySelectionWidget(QWidget):
    # This tool's scope key on the Ligands table it borrows (BoundTableWidget.push_scope).
    _SCOPE_KEY = "diversity"

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._running = False
        self._last_result: dict[str, Any] | None = None  # latest preview batch
        # A clustering run in flight: {job_id, run_id, scope_label, method, threshold, fp_*} —
        # registered as a result when job_finished fires for job_id.
        self._pending_cluster_job: dict[str, Any] | None = None
        self._job_signal_connected = False
        self._scope_signal_connected = False
        self._reset_accumulation()

        outer = QVBoxLayout(self)
        outer.addWidget(self._build_controls())
        self.scope_label = QLabel("", self)
        self.scope_label.setWordWrap(True)
        outer.addWidget(self.scope_label)
        self.stats_label = QLabel(
            "'Cluster' clusters the whole scope and opens the result in Clusters; nothing is excluded "
            "until you ask for it there. 'Preview sample' is an optional quick look to tune the "
            "method/threshold first.",
            self,
        )
        self.stats_label.setWordWrap(True)
        outer.addWidget(self.stats_label)
        outer.addStretch(1)

        self.set_combo.currentIndexChanged.connect(self._sync_ligands_scope)
        self.refresh()
        self._load_cache()  # restore a pile left from a previous visit (the plot redraws on show)

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
        # Full per-point graph data for the whole pile (x, y, molecule_id, cluster_id, is_centroid).
        self._all_points: list[tuple[float, float, int, int, bool]] = []

    # --- disk cache (previews survive a session until Clear) -------------------
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
        """Restore a saved pile, and set the scope/FP controls back to what produced
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
            scope_data, radius, nbits = self._basis_key[:3]
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
        if self._selections:
            self.stats_label.setText(
                f"Restored {len(self._selections)} cached preview sample(s) · {len(self._seen_ids)} "
                f"molecules. Press 'Cluster' to run over the whole scope."
            )

    def _delete_cache(self) -> None:
        path = self._cache_path()
        if path is not None:
            path.unlink(missing_ok=True)

    # --- controls -------------------------------------------------------------
    def _build_controls(self) -> QWidget:
        box = QGroupBox("Diversity", self)
        row = QHBoxLayout(box)

        # left: shared setup (what to cluster, feature, actions) --------------------------------
        setup = QFormLayout()
        self.set_combo = QComboBox(box)  # Selection scope (usage classes + sets)
        self.set_combo.setToolTip(
            "What to cluster. The Ligands table shows it while this tool is open: narrow it further "
            "there ('Select' on rows, column filters) and the run follows."
        )
        self.method_combo = QComboBox(box)
        for name in DiversityAPI.supported_methods():
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
            f"sample. To cluster the whole scope, use 'Cluster'."
        )
        self.run_button.clicked.connect(self._run)
        self.clear_button = QPushButton("Clear", box)
        self.clear_button.setToolTip("Forget the accumulated preview samples and start a fresh pile.")
        self.clear_button.clicked.connect(self._clear)
        self.size_dist_button = QPushButton("Size distribution", box)
        self.size_dist_button.setToolTip(
            "Histogram of compounds-per-cluster for the current previews — how many clusters are "
            "singletons vs. large. Replaces the scatter in the Distribution dock; Preview redraws it."
        )
        self.size_dist_button.clicked.connect(self._show_size_distribution)
        self.cluster_button = QPushButton("Cluster", box)
        self.cluster_button.setToolTip(_CLUSTER_TIP)
        self.cluster_button.clicked.connect(self._cluster)
        self.results_button = QPushButton("Show clusters", box)
        self.results_button.setToolTip("Open the Clusters view: the runs made so far, their clusters and ligands.")
        self.results_button.clicked.connect(lambda: self._show_clusters())
        buttons.addWidget(self.cluster_button)
        buttons.addWidget(self.results_button)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.size_dist_button)
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
        for name in DiversityAPI.supported_methods():
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

    def _ligands_table(self):
        central = getattr(self.window(), "central_widget", None)
        try:
            return central.open_view(LIGANDS_VIEW_ID) if central is not None else None
        except Exception:  # noqa: BLE001 - a missing/failed view must not break the tool
            return None

    def _sync_ligands_scope(self, *_args) -> None:
        """Show the Selection choice on the Ligands table, so the scope is something you can see."""
        if not self.isVisible():
            return  # off screen it borrows nothing; showEvent pushes the scope on return
        table = self._ligands_table()
        if table is None:
            return
        kind, value = self.set_combo.currentData() or ("all", None)
        filters, clause = [], None
        if kind == "usage":
            many = isinstance(value, tuple)
            filters = [FilterSpec(
                "usage_class", FilterOperator.IN if many else FilterOperator.EQ,
                list(value) if many else value, label="diversity_usage",
            )]
        elif kind == "set":
            molecules = self.runtime.molecules
            clause = molecules.scope_clause(molecules.select(source=molecules.resolve_set(value)))
        if not self._scope_signal_connected:
            table.records_changed.connect(self._on_scope_count)
            self._scope_signal_connected = True
        table.push_scope(self._SCOPE_KEY, filters=filters, clause=clause,
                         empty_message="No ligands in this scope")
        if table.record_count is not None:
            self._on_scope_count(table.record_count)  # a push that changed nothing reloads nothing

    def _on_scope_count(self, count: int) -> None:
        if self.isVisible():
            self.scope_label.setText(
                f"Scope: the {int(count)} ligand(s) the Ligands table shows. Narrow it there "
                f"('Select' on rows, column filters) or with Selection above."
            )

    def showEvent(self, event):
        super().showEvent(event)
        opener = getattr(self.window(), "open_or_focus_view", None)
        if callable(opener):
            opener(LIGANDS_VIEW_ID)  # the scope, on screen (a tab change also clears the chart)
        self._sync_ligands_scope()
        if self._selections:
            self._push_plot()

    def hideEvent(self, event):
        super().hideEvent(event)
        table = self._ligands_table()
        if table is not None:
            table.pop_scope(self._SCOPE_KEY)

    def _scope_params(self) -> dict[str, Any] | None:
        """The run's scope; None (after telling the user) when the table narrowing is too large."""
        kind, value = self.set_combo.currentData() or ("all", None)
        molecule_filters: dict[str, Any] = {}
        if kind == "usage":
            molecule_filters["usage_class__in" if isinstance(value, tuple) else "usage_class"] = (
                list(value) if isinstance(value, tuple) else value
            )
        table = self._ligands_table()
        ids = table.scope_ids() if table is not None else None  # None: nothing narrowed there
        if ids is not None:
            if len(ids) > MAX_NARROWED_IDS:
                QMessageBox.information(
                    self, "Diversity",
                    f"The Ligands table is narrowed to {len(ids)} rows — too many to pass as a list "
                    f"(limit {MAX_NARROWED_IDS}). Save them as a set ('Create Ligand Set…') and pick "
                    f"it in Selection, or clear the table filters.",
                )
                return None
            molecule_filters["id__in"] = ids or [0]
        return {
            "molecule_set": self.runtime.molecules.resolve_set(value) if kind == "set" else None,
            "molecule_filters": molecule_filters,
            "fp_radius": int(self.radius_spin.value()),
            "fp_nbits": int(self.nbits_spin.value()),
            "sample_limit": RUN_SAMPLE_LIMIT,
        }

    def _scope_key(self, scope_kw: dict[str, Any]) -> tuple:
        ids = scope_kw["molecule_filters"].get("id__in")
        # A narrowed table is another universe: its samples must not pile onto a previous one's.
        narrowed = (len(ids), sum(ids)) if ids is not None else None
        return (self.set_combo.currentData(), int(self.radius_spin.value()), int(self.nbits_spin.value()), narrowed)

    def _cluster_params(self) -> dict[str, Any]:
        method = str(self.method_combo.currentData() or "bitbirch")
        params: dict[str, Any] = {"method": method, "per_cluster": int(self.per_cluster_spin.value())}
        for key, getter in self._method_getters.get(method, {}).items():
            params[key] = getter()
        return params

    # --- preview (manual, off the GUI thread, with a blocking overlay) --------
    def _clear(self) -> None:
        self._reset_accumulation()
        self.stats_label.setText(
            "Cleared. 'Cluster' runs the clustering over the whole scope; 'Preview sample' is an "
            "optional look first."
        )
        self._push_plot()
        self._delete_cache()
        gc.collect()  # drop the freed scatter/data promptly (RSS may still lag — allocator-held)

    def _run(self) -> None:
        if self._running:
            return
        scope_kw = self._scope_params()
        if scope_kw is None:
            return
        key = self._scope_key(scope_kw)
        if key != self._basis_key:  # new scope/FP → the fixed PCA basis no longer applies
            self._reset_accumulation()
            self._basis_key = key
        if len(self._selections) >= MAX_PREVIEWS:
            QMessageBox.information(
                self, "Diversity",
                f"Preview limit ({MAX_PREVIEWS}) reached — press Clear to start a fresh pile.",
            )
            return
        self._running = True
        self.run_button.setEnabled(False)
        self.cluster_button.setEnabled(False)
        cluster_kw = self._cluster_params()
        seed = random.randrange(1 << 30)  # a fresh subset every press
        run_async(
            lambda: self._load_and_cluster(scope_kw, cluster_kw, seed),
            self._on_previewed,
            on_error=self._on_error,
            busy=self,
        )

    def _load_and_cluster(self, scope_kw: dict, cluster_kw: dict, seed: int) -> dict[str, Any]:
        universe = self.runtime.diversity.load_universe(
            seed=seed, basis=self._basis, exclude_ids=self._seen_ids, **scope_kw
        )
        analysis = self.runtime.diversity.cluster_loaded(universe, **cluster_kw)
        return {"basis": universe.basis, "analysis": analysis.to_mapping()}

    def _on_previewed(self, payload: dict[str, Any]) -> None:
        self._running = False
        self.run_button.setEnabled(True)
        self.cluster_button.setEnabled(self._pending_cluster_job is None)
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
                rep_cluster_ids.append(raw_label)
            else:
                self._pool_points.append(projection[i])
        color = _PALETTE[len(self._selections) % len(_PALETTE)]
        self._selections.append({
            "points": rep_pts,
            "ids": rep_ids,  # parallel to points → hover maps a marker back to its centroid molecule
            "cluster_ids": rep_cluster_ids,  # parallel to points
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
            f"{len(self._selections)} preview(s) — this is just a look. Press 'Cluster' to run "
            f"the clustering over the whole scope."
        )
        self._push_plot()
        self._save_cache()

    # --- plot lives in the shared Distribution dock ---------------------------
    def _push_plot(self) -> None:
        show = getattr(self.window(), "show_diversity_universe", None)
        if show is not None:
            show(self._pool_points, self._selections, self._evr, None)

    def _show_size_distribution(self) -> None:
        """Draw the compounds-per-cluster histogram over the current previews."""
        from amdockvs.diversity.clustering import size_histogram

        sizes = [int(c.get("size") or 0)
                 for group in self._selections for c in (group.get("clusters") or [])]
        if not sizes:
            QMessageBox.information(
                self, "Diversity",
                "No clusters yet — Preview a sample first. A finished run has its own in Clusters.",
            )
            return
        labels, counts = size_histogram(sizes)
        show = getattr(self.window(), "show_size_distribution", None)
        if show is not None:
            show(f"cluster size ({len(sizes)} clusters)", labels, counts)

    # --- the run: ALWAYS an mf job (serial=1 CPU or parallel multiround) -------------------------
    def _cluster(self) -> None:
        """Cluster the ENTIRE scope. This always goes to an mf clustering job (never inline —
        Preview is the only inline path). First size the scope off the GUI thread, then let the
        user confirm/override the CPU count and submit the job."""
        if self._running or self._pending_cluster_job is not None:
            return
        scope_kw = self._scope_params()
        if scope_kw is None:
            return
        self._running = True
        self.run_button.setEnabled(False)
        self.cluster_button.setEnabled(False)
        scope_kw.pop("sample_limit", None)  # the run clusters everything
        cluster_kw = self._cluster_params()
        self.stats_label.setText("Sizing the scope…")
        run_async(
            lambda: self.runtime.diversity.scope_count(
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

        from amdockvs.diversity.api import plan_cpus

        self._running = False
        self.run_button.setEnabled(True)
        if n <= 0:
            self.cluster_button.setEnabled(True)
            self.stats_label.setText("No fingerprintable ligands in this scope.")
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
            self.cluster_button.setEnabled(True)
            self.stats_label.setText("Run cancelled.")
            return
        cpus = int(cpus)
        run_id = uuid.uuid4().hex
        method = str(cluster_kw.get("method") or "bitbirch")
        threshold = float(cluster_kw.get("threshold") or 0.35)
        try:
            job_id = self.runtime.diversity.cluster_job(
                method=method, threshold=threshold, per_cluster=int(cluster_kw.get("per_cluster") or 1),
                molecule_set=scope_kw["molecule_set"], molecule_filters=scope_kw["molecule_filters"],
                fp_radius=scope_kw["fp_radius"], fp_nbits=scope_kw["fp_nbits"],
                cluster_run_id=run_id, num_cpus=cpus,
            )
        except Exception as exc:  # noqa: BLE001 — submission failure must not wedge the button
            self.cluster_button.setEnabled(True)
            QMessageBox.critical(self, "Diversity", f"Could not submit the clustering job: {exc}")
            return
        narrowed = "id__in" in scope_kw["molecule_filters"]
        self._pending_cluster_job = {
            "job_id": str(job_id), "run_id": run_id, "method": method, "threshold": threshold,
            "scope_label": self.set_combo.currentText() + (" (narrowed in the table)" if narrowed else ""),
            "fp_radius": int(scope_kw["fp_radius"]), "fp_nbits": int(scope_kw["fp_nbits"]),
        }
        self._connect_job_signal()
        mode = "parallel multiround" if cpus > 1 else "serial"
        self.stats_label.setText(
            f"Clustering {n} ligands as an mf job on {cpus} CPU(s) ({mode}) — see Jobs. The result "
            f"opens in Clusters when it finishes; you can keep working."
        )

    def _connect_job_signal(self) -> None:
        if self._job_signal_connected:
            return
        bridge = getattr(self.window(), "monitor_bridge", None)
        if bridge is not None:
            bridge.job_finished.connect(self._on_cluster_job_finished)
            self._job_signal_connected = True

    def _on_cluster_job_finished(self, job_id: str, status: str) -> None:
        # Not gated on isVisible(): this fires once per run, and a run that finished while another
        # tool had the panel still has to be registered.
        pending = self._pending_cluster_job
        if not pending or str(job_id) != pending["job_id"]:
            return
        self._pending_cluster_job = None
        if str(status or "").strip().lower() != "completed":
            self.cluster_button.setEnabled(True)
            self.stats_label.setText(f"Clustering job {status} — no result. See Jobs.")
            return
        run_async(
            lambda p=pending: self._register_run(p),
            self._on_registered,
            on_error=self._on_error,
            busy=self.stats_label,
            compact=True,
        )

    def _register_run(self, pending: dict[str, Any]) -> str:
        """Off-thread: register the finished run as a result. The job already wrote the graph
        sidecar (PCA + clusters), so this just reads it — nothing is recomputed."""
        self.runtime.diversity.register_run_from_sidecar(
            pending["run_id"], method=pending["method"], threshold=pending["threshold"],
            scope_label=pending["scope_label"],
            fp_radius=int(pending["fp_radius"]), fp_nbits=int(pending["fp_nbits"]),
        )
        return pending["run_id"]

    def _on_registered(self, run_id: str) -> None:
        self.cluster_button.setEnabled(True)
        self.stats_label.setText(
            "Clustering done. Its clusters are in the Clusters view: use the representatives as the "
            "selection, exclude the rest, or save them as a set from there."
        )
        self._show_clusters(run_id, focus=self.isVisible())

    def _show_clusters(self, run_id: str | None = None, *, focus: bool = True) -> None:
        """Open the Clusters view (on a run, when given). Without focus an open view is only told
        about the run — a job that ended behind another tool must not steal the screen."""
        window = self.window()
        central = getattr(window, "central_widget", None)
        opener = getattr(window, "open_or_focus_view", None)
        view = opener(CLUSTERS_VIEW_ID) if focus and callable(opener) else (
            central.open_view(CLUSTERS_VIEW_ID) if central is not None else None
        )
        if run_id and view is not None:
            view.show_run(run_id)

    def _on_error(self, exc: Exception) -> None:
        self._running = False
        self.run_button.setEnabled(True)
        self.cluster_button.setEnabled(self._pending_cluster_job is None)
        QMessageBox.critical(self, "Diversity", str(exc))


def register_selection_workspace(window) -> None:
    window.register_main_view(
        SELECTION_VIEW_ID,
        "Diversity",
        lambda: DiversitySelectionWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = ["SELECTION_VIEW_ID", "DiversitySelectionWidget", "register_selection_workspace"]
