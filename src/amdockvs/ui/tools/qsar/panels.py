"""QSAR workspace panels over the lean runtime.qsar API.

Thin Qt over the script-mode APIs (which already work standalone): edit activities, compute
descriptors, train/predict/evaluate model templates, and run high-throughput pre-docking
filtering. Heavy calls go through run_async so the GUI never blocks.
"""
from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.models import MoleculeRecord, QSARModelRecord
from amdockvs.qsar.build import FEATURE_SOURCES
from amdockvs.qsar.model_analysis import member_columns, member_scope, model_scores
from amdockvs.qsar.modeling import AD_K, AD_THRESHOLD, supported_algorithms
from amdockvs.qsar.splitters import SPLITTERS
from amdockvs.ui.catalog.common import (
    PREVIEW_2D_COLUMN_FIELD,
    molecule_2d_preview_paint_for_runtime,
    molecule_2d_preview_tooltip,
    project_table,
)
from amdockvs.ui.common.async_query import run_async
from ms_components.chips import FlowLayout
from ms_components.ms_stepper import Orientation, QStepper
from ms_components.ms_table import AlignHint, ColumnDef, ColumnKind, SortSpec, TableConfig, TableLoadMode
from ms_components.ms_table.table_model import RAW_OBJECT_ROLE
from amdockvs.ui.view_ids import QSAR_ANALYSIS_VIEW_ID, QSAR_BUILD_VIEW_ID


# ponytail: a selection is an `id IN [...]` filter, and SQLite binds at most 32766 values in its
# stock build. A larger cut is applied with "Exclude the rest" instead.
_MAX_SELECTION = 30000


def _scrolled(inner: QWidget) -> QScrollArea:
    """Scroll wrapper so the tall wizard / tables never deform the area that hosts them."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.NoFrame)
    scroll.setWidget(inner)
    return scroll


class _FreshCombo(QComboBox):
    """Re-reads its items each time its list opens: endpoints arrive from imports, the Activity
    view and deletions, none of which tells this tool."""

    def __init__(self, reload, parent=None):
        super().__init__(parent)
        self._reload = reload

    def showPopup(self) -> None:
        self._reload()
        super().showPopup()


def _form_step(rows: list[tuple[str, QWidget]]) -> QWidget:
    """A small QWidget with a QFormLayout — the body for one stepper step."""
    page = QWidget()
    form = QFormLayout(page)
    for label, widget in rows:
        if label:
            form.addRow(label, widget)
        else:
            form.addRow(widget)
    return page


# ---------------------------------------------------------------------------
# Build model (left-bar tool)
# ---------------------------------------------------------------------------

class QSARBuildWidget(QWidget):
    """The build-a-model wizard. It only builds: what it trains is read in QSAR Analysis."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(_scrolled(self._train_box()), 1)
        outer.addWidget(self.status)  # the endpoints load in showEvent

    def refresh(self) -> None:
        run_async(self.runtime.qsar.endpoint_kinds, self._fill_endpoints, on_error=lambda _e: None,
                  busy=self.stepper)

    refresh_view = refresh

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def _preview_split(self) -> None:
        endpoint = self.train_endpoint.currentText().strip()
        if not endpoint:
            QMessageBox.information(self, "QSAR", "Pick an endpoint (Data step) first.")
            return
        show = getattr(self.window(), "show_split_distribution", None)
        if show is None:
            return
        split = self.split_combo.currentText()
        test_size = float(self.test_size.value())
        # only threshold a continuous endpoint into classes; categorical uses labels directly
        threshold = (float(self.class_threshold.value())
                     if self._endpoint_kinds.get(endpoint) != "categorical"
                        and self.task_combo.currentText() == "classification" else None)
        self.status.setText(f"Previewing {split} split for {endpoint}… (computing scaffolds can take a moment)")

        def _done(res):
            classes = threshold is not None or self._endpoint_kinds.get(endpoint) == "categorical"
            show(f"{endpoint} - {split} split (train vs test)", res["categories"],
                 {"train": res["train"], "test": res["test"]}, "Class" if classes else f"{endpoint} range")
            self.status.setText(f"Split preview [{split}] {endpoint}: "
                                f"{res['n_train']} train / {res['n_test']} test - {res['note']}")

        run_async(
            lambda: self.runtime.qsar.split_preview(
                endpoint=endpoint, split=split, test_size=test_size, class_threshold=threshold),
            _done,
            on_error=lambda exc: QMessageBox.warning(self, "QSAR split preview", str(exc)),
            busy=self,
        )

    # --- training -------------------------------------------------------------
    def _train_box(self) -> QGroupBox:
        """Build-a-model wizard: a QStepper (same component the docking studio uses) over the
        same train controls, laid out as StarDrop's Auto-Modeler steps."""
        box = QGroupBox(self)
        layout = QVBoxLayout(box)

        # ponytail: a synchronous DISTINCT over the activities table, small next to the ligands.
        self.train_endpoint = _FreshCombo(lambda: self._fill_endpoints(self.runtime.qsar.endpoint_kinds()), box)
        self.train_endpoint.setEditable(True)
        self._endpoint_kinds: dict[str, str] = {}
        self.train_endpoint.currentTextChanged.connect(self._default_task_for_endpoint)
        self.task_combo = QComboBox(box)
        self.task_combo.addItems(["regression", "classification"])
        self.task_combo.currentTextChanged.connect(self._sync_algorithms)
        self.class_threshold = QDoubleSpinBox(box)
        self.class_threshold.setRange(-1e6, 1e6)
        self.class_threshold.setDecimals(3)
        self.split_combo = QComboBox(box)
        self.split_combo.addItems(list(SPLITTERS))
        self.split_combo.setToolTip(
            "random · stratified (keeps each class / value quantile in proportion) · scaffold (whole "
            "Bemis-Murcko scaffolds) · butina (Taylor-Butina clusters) · maxmin (groups around diverse "
            "seeds). The grouped ones keep a group on one side of the holdout and of every CV fold.")
        self.test_size = QDoubleSpinBox(box)
        self.test_size.setRange(0.0, 0.9)
        self.test_size.setSingleStep(0.1)
        self.test_size.setValue(0.3)
        split_preview_btn = QPushButton("Preview split distribution", box)
        split_preview_btn.setToolTip("Class/value distribution in train vs test - scaffold split balances "
                                     "chemical space but NOT the labels, so check the actives aren't lopsided.")
        split_preview_btn.clicked.connect(self._preview_split)
        self.feature_source = QComboBox(box)
        self.feature_source.addItems(list(FEATURE_SOURCES))
        self.feature_source.setToolTip(
            "rdkit2d: full RDKit ~200 block, auto-scaled and correlation-pruned inside the CV pipeline · "
            "ecfp4: Morgan fingerprint. Training computes what a molecule lacks first, as a job.")
        self.feature_source.currentTextChanged.connect(self._sync_features)
        self.fp_radius = QSpinBox(box)
        self.fp_radius.setRange(1, 4)
        self.fp_radius.setValue(2)
        self.fp_radius.setToolTip("Morgan radius: 2 is ECFP4.")
        self.fp_nbits = QSpinBox(box)
        self.fp_nbits.setRange(256, 16384)
        self.fp_nbits.setSingleStep(1024)
        self.fp_nbits.setValue(2048)
        self.fp_nbits.setToolTip("Fingerprint length in bits.")
        self.corr_threshold = QDoubleSpinBox(box)
        self.corr_threshold.setRange(0.5, 1.0)
        self.corr_threshold.setSingleStep(0.05)
        self.corr_threshold.setValue(0.95)
        self.corr_threshold.setToolTip("Drop one of every descriptor pair with |r| above this (ignored for ecfp4).")
        self.y_scramble = QSpinBox(box)
        self.y_scramble.setRange(0, 50)
        self.y_scramble.setToolTip("y-randomisation: refit on N shuffled-label sets (CV-scored). "
                                   "Should score ~0; if near the real score, the model is chance. 0 = off.")
        self.corr_btn = corr_btn = QPushButton("Analyze correlation…", box)
        corr_btn.setToolTip("Heatmap of THIS feature block (above) at THIS cutoff - shows which "
                            "descriptors the pipeline prunes as redundant before training.")
        corr_btn.clicked.connect(self._show_correlation)
        self.algo_box = QWidget(box)
        self.algo_box_layout = QVBoxLayout(self.algo_box)
        self.algo_box_layout.setContentsMargins(0, 0, 0, 0)
        self.algo_checks: dict[str, QCheckBox] = {}
        self.cv_folds = QSpinBox(box)
        self.cv_folds.setRange(0, 20)
        self.cv_folds.setToolTip("k-fold cross-validation (0 = off). Reports Q² for regression.")

        # Navigate only via the Back/Next buttons (header_nav=False blocks clicking a step);
        # the last step's button reads "Train" and runs training (finished signal).
        self.stepper = QStepper(orientation=Orientation.HORIZONTAL, linear=False,
                                alternative_labels=True, show_navigation=True, parent=box)
        self.stepper.finish_text = "Train"
        self.stepper.finished.connect(self._train)
        self.stepper.add_step("Data", "endpoint & task", header_nav=False).add_widget(
            _form_step([("Endpoint", self.train_endpoint), ("Task", self.task_combo),
                        ("Class threshold", self.class_threshold)]))
        self.stepper.add_step("Split", "train/test", header_nav=False).add_widget(
            _form_step([("Split", self.split_combo), ("Test split", self.test_size), ("", split_preview_btn)]))
        self._features_page = _form_step([
            ("Features", self.feature_source), ("Radius", self.fp_radius), ("Bits", self.fp_nbits),
            ("Correlation cutoff", self.corr_threshold), ("", corr_btn)])
        self.stepper.add_step("Descriptors", "features", header_nav=False).add_widget(self._features_page)
        self.stepper.add_step("Methods", "algorithms & run", header_nav=False).add_widget(
            _form_step([("Algorithms", self.algo_box), ("CV folds", self.cv_folds),
                        ("y-scramble runs", self.y_scramble)]))
        layout.addWidget(self.stepper)
        self._sync_algorithms()
        self._sync_features()
        return box

    def _sync_features(self) -> None:
        """Radius and bits are the fingerprint's; the correlation cutoff prunes descriptors."""
        form = self._features_page.layout()
        ecfp4 = self.feature_source.currentText() == "ecfp4"
        for widget, visible in ((self.fp_radius, ecfp4), (self.fp_nbits, ecfp4),
                                (self.corr_threshold, not ecfp4), (self.corr_btn, not ecfp4)):
            form.setRowVisible(widget, visible)

    def _sync_algorithms(self) -> None:
        """Rebuild the algorithm checkboxes for the current task (tick several to train + compare)."""
        for cb in self.algo_checks.values():
            cb.setParent(None)
        self.algo_checks.clear()
        for i, name in enumerate(supported_algorithms(self.task_combo.currentText())):
            cb = QCheckBox(name, self.algo_box)
            cb.setChecked(i == 0)
            self.algo_box_layout.addWidget(cb)
            self.algo_checks[name] = cb
        is_categorical = self._endpoint_kinds.get(self.train_endpoint.currentText().strip()) == "categorical"
        self.class_threshold.setEnabled(self.task_combo.currentText() == "classification" and not is_categorical)
        self.class_threshold.setToolTip(
            "Categorical endpoint: labels are used directly, no threshold." if is_categorical
            else "Binarize a continuous endpoint: active = value ≥ threshold.")

    def _train(self) -> None:
        endpoint = self.train_endpoint.currentText().strip()
        if not endpoint:
            QMessageBox.information(self, "QSAR", "Choose an endpoint to train on.")
            return
        algos = [name for name, cb in self.algo_checks.items() if cb.isChecked()]
        if not algos:
            QMessageBox.information(self, "QSAR", "Tick at least one algorithm.")
            return
        task = self.task_combo.currentText()
        # Categorical endpoints already carry 0/1 labels — the value IS the class, so DON'T
        # threshold: a default 0.0 threshold makes every 0/1 sample >=0 → one class → acc=1.0.
        # Thresholding only makes sense to binarize a *continuous* endpoint into classes.
        needs_threshold = task == "classification" and self._endpoint_kinds.get(endpoint) != "categorical"
        base = dict(
            endpoint=endpoint,
            task=task,
            feature_source=self.feature_source.currentText(),
            split=self.split_combo.currentText(),
            test_size=float(self.test_size.value()),
            cv_folds=int(self.cv_folds.value()),
            corr_threshold=float(self.corr_threshold.value()),
            y_scramble=int(self.y_scramble.value()),
            class_threshold=float(self.class_threshold.value()) if needs_threshold else None,
        )
        if base["feature_source"] == "ecfp4":
            base.update(fp_radius=int(self.fp_radius.value()), fp_nbits=int(self.fp_nbits.value()))

        def _done(request: dict) -> None:
            self.stepper.setEnabled(True)
            reused = ", ".join(f"{algo} (#{mid})" for algo, mid in request["existing"].items())
            self.status.setText(
                (f"Training {len(algos) - len(request['existing'])} model(s) as a job; follow them in "
                 "QSAR Analysis." if request["job_id"] else "Nothing to train.")
                + (f" Already trained on the same data and settings: {reused}." if reused else ""))
            open_view = getattr(self.window(), "open_or_focus_view", None)
            if open_view is not None:
                open_view(QSAR_ANALYSIS_VIEW_ID).show_trained(
                    {**request, "endpoint": endpoint, "task": task, "features": base["feature_source"],
                     "split": base["split"]})

        self.stepper.setEnabled(False)
        self.status.setText("Labeling and splitting the molecules…")
        # The split is checked here, so a split that cannot work fails before any job is queued.
        run_async(lambda: self.runtime.qsar.submit_train(algorithm=algos, **base), _done,
                  on_error=self._on_train_error, busy=self)

    def _on_train_error(self, exc: Exception) -> None:
        self.stepper.setEnabled(True)
        self.status.setText("")
        QMessageBox.warning(self, "QSAR train", str(exc))

    def _show_correlation(self) -> None:
        show = getattr(self.window(), "show_correlation_heatmap", None)
        if show is None:
            return
        source = self.feature_source.currentText()  # the block chosen in the wizard's Descriptors step
        thr = float(self.corr_threshold.value())

        def _done(res):
            show(f"{source} correlation", res["labels"], res["matrix"])
            dropped = res["dropped"]
            preview = ", ".join(dropped[:12]) + (" …" if len(dropped) > 12 else "")
            self.status.setText(
                f"{source}: {len(res['labels'])} features over {res['n_ligands']} ligands. The pipeline "
                f"keeps {len(res['labels']) - len(dropped)} and prunes {len(dropped)} redundant at |r|>{thr:.2f}"
                + (f": {preview}" if dropped else "."))

        run_async(
            lambda: self.runtime.qsar.correlation_matrix(feature_source=source, corr_threshold=thr),
            _done,
            on_error=self._on_correlation_error,
            busy=self,
        )

    def _on_correlation_error(self, exc: Exception) -> None:
        if "RDKit 2D block" not in str(exc):
            QMessageBox.warning(self, "QSAR correlation", str(exc))
            return
        if QMessageBox.question(self, "QSAR correlation", "The RDKit 2D block is not computed for these "
                                "ligands yet. Compute it now, as a job?") == QMessageBox.Yes:
            job_id = self.runtime.qsar.compute_descriptors(rdkit2d=True)
            self.status.setText(f"RDKit 2D block job submitted ({job_id}); analyze again when it ends.")

    def _fill_endpoints(self, kinds: dict[str, str]) -> None:
        self._endpoint_kinds = dict(kinds)
        current = self.train_endpoint.currentText()
        self.train_endpoint.blockSignals(True)
        self.train_endpoint.clear()
        self.train_endpoint.addItems(sorted(kinds))
        if current:
            self.train_endpoint.setCurrentText(current)
        self.train_endpoint.blockSignals(False)
        self._default_task_for_endpoint(self.train_endpoint.currentText())

    def _default_task_for_endpoint(self, endpoint: str) -> None:
        """Default the task to match how the endpoint was ingested (categorical → classification)."""
        kind = self._endpoint_kinds.get(str(endpoint).strip())
        if kind:
            self.task_combo.setCurrentText("classification" if kind == "categorical" else "regression")
        self._sync_algorithms()  # refresh threshold enable/tooltip even when the task didn't change


# ---------------------------------------------------------------------------
# Model analysis (catalog view)
# ---------------------------------------------------------------------------

_METRIC_NAMES = {
    "accuracy": "Accuracy", "balanced_accuracy": "Balanced accuracy", "f1": "F1", "mcc": "MCC",
    "roc_auc": "ROC AUC", "pr_auc": "PR AUC", "r2": "R²", "rmse": "RMSE", "mae": "MAE", "mse": "MSE",
    "enrichment_1pct": "Enrichment at 1%", "cv_mcc": "CV MCC", "cv_accuracy": "CV accuracy",
}


def _metric_name(key: str) -> str:
    return _METRIC_NAMES.get(key, key.replace("_", " ").capitalize())


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.3f}"


# What a training job's status reads as in the training line.
_JOB_STATUS = {"pending": "queued", "running": "training", "failed": "failed", "canceled": "canceled"}
# Observed vs predicted: colour = model, marker = subset (circle, triangle, square).
_SUBSET_SYMBOLS = {"train": "o", "test": "t", "cv": "s"}
_FEATURES = {"fingerprints_ecfp4": "ecfp4", "descriptors_2d": "rdkit2d"}


def _models_config() -> TableConfig:
    test_score, cv_score = model_scores()
    number = dict(kind=ColumnKind.NUMBER, align=AlignHint.RIGHT, formatter=_fmt, width=150)
    count = dict(kind=ColumnKind.INTEGER, align=AlignHint.RIGHT, width=110)
    return TableConfig(
        model_class=QSARModelRecord,
        columns=[
            ColumnDef("id", label="ID", kind=ColumnKind.INTEGER, align=AlignHint.RIGHT, width=70),
            ColumnDef("name", label="Model", width=200),
            ColumnDef("task", label="Task", width=120),
            ColumnDef("algorithm", label="Algorithm", width=160),
            ColumnDef("feature_type", label="Features", width=125, formatter=lambda v: _FEATURES.get(v, v)),
            ColumnDef("target", label="Endpoint", width=125),
            ColumnDef("splitter", label="Split", width=110),
            ColumnDef("n_train", label="Train n", **count),
            ColumnDef("n_test", label="Test n", **count),
            ColumnDef("test_score", label="Test R² / MCC", expr=test_score, **number),
            ColumnDef("cv_score", label="Q² / AUC", expr=cv_score, **number),
            ColumnDef("source", label="Source", width=110, visible=False),
        ],
        default_sort=[SortSpec("id", descending=True)],
        load_mode=TableLoadMode.INFINITE,
        empty_message="No QSAR models in this project yet. Build one in the QSAR tool.",
    )


def _empty_subset(record, subset: str) -> str:
    """Why a model has no molecules in ``subset``."""
    if record is None:
        return "Select a model to list its molecules."
    metrics = record.metrics or {}
    if subset == "cv":
        return ("No out-of-fold predictions: " + str(metrics["cv_warning"]) if metrics.get("cv_warning")
                else "This model was not cross-validated (CV folds = 0).")
    if subset == "test":
        return "This model has no holdout test set (test fraction = 0)."
    if record.source == "external":
        return "An external model records no molecules; evaluate it on labeled ones."
    return "No molecules in this subset."


def _members_config(runtime, model_id: int, subset: str, empty: str = "") -> tuple[TableConfig, object]:
    """(config, clause): the molecules of one model in ``subset``, with what the model said about
    them. Built per model: the values are subqueries on that model's member rows."""
    values = member_columns(model_id, subset)
    number = dict(kind=ColumnKind.NUMBER, align=AlignHint.RIGHT, formatter=_fmt, width=120)
    config = TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef(PREVIEW_2D_COLUMN_FIELD, label="2D Image", width=96, min_width=96, max_width=240,
                      sortable=False, filterable=False, searchable=False, align=AlignHint.CENTER,
                      paint_factory=molecule_2d_preview_paint_for_runtime(runtime),
                      tooltip=molecule_2d_preview_tooltip, cell_height=84, visible=False),
            ColumnDef("name", label="Molecule", width=180),
            ColumnDef("role", label="Subset", expr=values["role"], width=110),
            ColumnDef("observed", label="Observed", expr=values["observed"], **number),
            ColumnDef("predicted", label="Predicted (out-of-fold)" if subset == "cv" else "Predicted",
                      expr=values["predicted"], **number),
        ],
        load_mode=TableLoadMode.INFINITE,
        multi_select=False,
        empty_message=empty or "Select a model to list its molecules.",
    )
    return config, member_scope(model_id, subset)


class QSARModelsWidget(QWidget):
    """Model analysis: the trained models, a summary of the selected one, the charts read off the
    selection (they follow it) and the molecules of the analysed subset."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._requests: list[dict] = []  # trainings submitted from Build: running, or failed
        self._job_status: dict[str, str] = {}
        self._to_select: set[str] = set()  # identities to select once they are stored
        self._selected_ids: set[int] = set()
        self._reloading = True  # a reload clears the table's selection: keep ours meanwhile
        self._pick_first = True  # the first load selects the newest model
        self._member_values: dict[int, dict] = {}
        self._members_key = None
        self._chart: str | None = None  # the chart last drawn: it follows the selection
        self._draws = 0  # only the latest draw lands; a slower earlier read is dropped
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._poll_jobs)

        layout = QVBoxLayout(self)
        self.training_line = QLabel("", self)
        self.training_line.setWordWrap(True)
        self.training_line.hide()
        layout.addWidget(self.training_line)
        bundles = QHBoxLayout()
        bundles.addStretch(1)
        for text, tip, slot in (
            ("Import model…", "Add a model exported from AMDockVS (a .zip bundle).", self._import_model),
            ("Export model…", "Save the selected model as a .zip bundle another project can import.",
             self._export_model),
        ):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bundles.addWidget(button)
        layout.addLayout(bundles)
        self.models_table = project_table(runtime, _models_config(), self)
        self.models_table.setMaximumHeight(240)
        self.models_table.selection_changed.connect(self._on_selection)
        self.models_table.data_refreshed.connect(self._reselect)
        layout.addWidget(self.models_table)

        self.model_summary = QLabel("Select a model row to review its results.", self)
        self.model_summary.setWordWrap(True)
        self.model_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.model_summary.setStyleSheet("QLabel { padding: 8px; border: 1px solid palette(mid); border-radius: 4px; }")
        layout.addWidget(self.model_summary)
        layout.addWidget(self._analyze_bar())

        layout.addWidget(QLabel("Molecules of the analysed subset: pick one to find it in the chart.", self))
        self._members_host = QVBoxLayout()
        self._members_host.setContentsMargins(0, 0, 0, 0)
        self.members_table = None
        layout.addLayout(self._members_host, 1)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._load_members()

    def _analyze_bar(self) -> QWidget:
        bar = QWidget(self)
        flow = FlowLayout(bar)
        flow.setContentsMargins(0, 0, 0, 0)
        # Charts are scored on the held-out test (honest predictivity), the out-of-fold training
        # predictions, the training set (its gap to test = overfitting) or everything.
        self.eval_subset = QComboBox(bar)
        for text, data in (("Test", "test"), ("Cross-validation", "cv"), ("Train", "train"), ("All", "all")):
            self.eval_subset.addItem(text, data)
        self.eval_subset.setToolTip("The molecules the charts score on and the table lists. Test = honest "
                                    "predictivity; Cross-validation = out-of-fold training predictions; "
                                    "Train vs Test gap = overfitting.")
        self.eval_subset.currentIndexChanged.connect(self._on_subset)
        flow.addWidget(QLabel("Analyze on:", bar))
        flow.addWidget(self.eval_subset)
        self._actions: dict[str, QPushButton] = {}
        for key, text, tip in (
            ("plot", "Observed vs predicted", "Fit scatter (regression): colour = model, marker = subset."),
            ("importance", "Feature importance", "Tree importances or linear weights of the selected model."),
            ("roc", "ROC curve", "ROC + AUC (classification); several selected models overlay."),
            ("ad", "Applicability domain", "Tanimoto of the ligands to the selected model's training set."),
        ):
            button = QPushButton(text, bar)
            button.setToolTip(tip)
            button.clicked.connect(lambda _c=False, k=key: self._draw(k))
            flow.addWidget(button)
            self._actions[key] = button
        self._update_action_state()
        return bar

    # --- bundles --------------------------------------------------------------
    def _export_model(self) -> None:
        models = self._selected_models()
        if len(models) != 1:
            QMessageBox.information(self, "QSAR", "Select one model to export.")
            return
        model = models[0]
        path, _ = QFileDialog.getSaveFileName(self, "Export QSAR model", f"{model.name}.zip", "Model bundles (*.zip)")
        if not path:
            return
        run_async(lambda: self.runtime.qsar.export_model(model=int(model.id), path=path),
                  lambda out: self.status.setText(f"Model #{model.id} exported to {out}."),
                  on_error=lambda exc: QMessageBox.warning(self, "Export QSAR model", str(exc)))

    def _import_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import QSAR model", "", "Model bundles (*.zip)")
        if not path:
            return
        # warn, do not block: joblib runs code while loading
        if QMessageBox.question(
            self, "Import QSAR model",
            "A model bundle holds a pickled model: loading it can run code. Import it only if you trust "
            "where it came from.\n\nImport it?",
        ) != QMessageBox.Yes:
            return

        def _done(result):
            model = result["model"]
            if result["existing"]:
                text = f"This project already has that model: #{model.id} {model.name}."
            else:
                text = f"Imported #{model.id} {model.name}: {result['members']} molecule(s) found here"
                text += f", {result['unmatched']} not in this project." if result["unmatched"] else "."
            self.status.setText(text)
            self._selected_ids = {int(model.id)}
            self.refresh()

        run_async(lambda: self.runtime.qsar.import_model(path=path), _done,
                  on_error=lambda exc: QMessageBox.warning(self, "Import QSAR model", str(exc)))

    # --- the table ------------------------------------------------------------
    def refresh(self) -> None:
        self._reloading = True
        self.models_table.refresh()
        self._refresh_training_line()

    refresh_view = refresh

    def show_trained(self, request: dict) -> None:
        """Called by the Build tool with what ``submit_train`` returned (plus endpoint, task,
        features, split): what it trains is selected as each model is stored."""
        self._to_select = set(request["identities"].values())
        self._selected_ids = set()
        self._pick_first = False  # what it trains gets selected instead
        if request.get("job_id"):
            self._requests.append(request)
            self._poll.start()
        self.refresh()

    def _poll_jobs(self) -> None:
        jobs = [j for r in self._requests if not r.get("ended")
                for j in (r.get("descriptor_job_id"), r["job_id"]) if j]
        if not jobs:
            self._poll.stop()
            return

        def _read():
            return {j: getattr(self.runtime.get_job_status(j), "status", "") for j in jobs}

        run_async(_read, self._on_job_status, on_error=lambda _e: None)

    def _on_job_status(self, statuses: dict[str, str]) -> None:
        changed = statuses != {j: self._job_status.get(j) for j in statuses}
        self._job_status.update(statuses)
        ended = [r for r in self._requests if statuses.get(r["job_id"]) in {"completed", "failed", "canceled"}]
        for request in ended:  # a failed training stays in the line; the monitor has its error
            request["ended"] = True
            if statuses[request["job_id"]] == "completed":
                self._requests.remove(request)
        if changed or ended:
            self.refresh()

    def _refresh_training_line(self) -> None:
        if not self._requests:
            self.training_line.hide()
            return

        def _stored():
            return {(m.metrics or {}).get("identity") for m in self.runtime.qsar.list_models()}

        run_async(_stored, self._show_training_line, on_error=lambda _e: None)

    def _show_training_line(self, stored: set) -> None:
        parts = []
        for request in self._requests:
            features_job = request.get("descriptor_job_id")
            if features_job and self._job_status.get(features_job) in {"pending", "running"}:
                state = "computing features"
            else:
                state = _JOB_STATUS.get(self._job_status.get(request["job_id"], "pending"), "queued")
            parts += [f"{algo}_{request.get('endpoint', '')} ({state})"
                      for algo, identity in request["identities"].items()
                      if identity not in stored and algo not in request["existing"]]
        self.training_line.setText("Training: " + " · ".join(parts) + (
            " - the Jobs monitor has the error." if "failed)" in " ".join(parts) else ""))
        self.training_line.setVisible(bool(parts))

    def _reselect(self, _total: int = 0) -> None:
        """After a reload: select again what was selected, plus the models just trained."""
        if not self._reloading:  # a further page of the same rows
            return
        view = self.models_table.view
        model, selection = view.model(), view.selectionModel()
        picked = set()
        for r in range(model.rowCount()):
            raw = model.index(r, 0).data(RAW_OBJECT_ROLE)
            if raw is None:
                continue
            identity = (raw.metrics or {}).get("identity")
            if int(raw.id) in self._selected_ids or identity in self._to_select:
                selection.select(model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
                picked.add(identity)
        if self._pick_first and model.rowCount() and model.index(0, 0).data(RAW_OBJECT_ROLE) is not None:
            if not picked:
                selection.select(model.index(0, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
            self._pick_first = False
        self._to_select -= picked
        self._reloading = False
        self._on_selection(self.models_table.get_selected_objects())

    def _selected_models(self) -> list:
        return sorted(self.models_table.get_selected_objects(), key=lambda m: int(m.id))

    def _on_selection(self, models) -> None:
        if self._reloading:
            return
        models = sorted(models, key=lambda m: int(m.id))
        self._selected_ids = {int(m.id) for m in models}
        self._update_action_state()
        if models:
            self._show_summary(models[0], len(models))
        else:
            self.model_summary.setText("Select a model row to review its results.")
        self._load_members()
        self._redraw()

    def _on_subset(self) -> None:
        self._load_members()
        self._redraw()

    def _update_action_state(self) -> None:
        if not getattr(self, "_actions", None):
            return
        for key, enabled in self._allowed(self._selected_models()).items():
            self._actions[key].setEnabled(enabled)

    @staticmethod
    def _allowed(models) -> dict[str, bool]:
        tasks = {m.task for m in models}
        return {"plot": bool(models) and tasks == {"regression"},
                "roc": bool(models) and tasks == {"classification"},
                "importance": len(models) == 1, "ad": len(models) == 1}

    # --- the summary card -----------------------------------------------------
    def _show_summary(self, record, n_selected: int) -> None:
        m = record.metrics or {}

        def metric_lines(values):
            return "<br>".join(
                f"<b>{_metric_name(key)}</b> {value:.3f}" if isinstance(value, float)
                else f"<b>{_metric_name(key)}</b> {value}"
                for key, value in (values or {}).items() if key != "n_samples" and value is not None
            ) or "—"

        blocks = [f"<b>{record.name}</b> &nbsp;·&nbsp; {record.task.title()} &nbsp;·&nbsp; {record.algorithm}"
                  f" &nbsp;·&nbsp; {str(m.get('feature_kind', '—')).upper()}<br>Endpoint: {record.target}"
                  f" &nbsp;·&nbsp; Split: {record.splitter or '—'}", "<hr>"]
        if m.get("train") or m.get("test"):
            blocks.append(
                "<table width='100%'><tr>"
                f"<th align='left'>Training (n={record.n_train})</th>"
                f"<th align='left'>Holdout test (n={record.n_test})</th></tr><tr>"
                f"<td valign='top'>{metric_lines(m.get('train'))}</td>"
                f"<td valign='top'>{metric_lines(m.get('test'))}</td></tr></table>")
        cv = []
        if m.get("cv_folds"):
            cv.append(f"{m['cv_folds']} folds on the training molecules")
        for key, label in (("q2", "Pooled out-of-fold Q²"), ("cv_r2_mean", "Mean fold R²"), ("cv_rmse", "CV RMSE"),
                           ("cv_mcc", "CV MCC"), ("cv_accuracy", "CV accuracy")):
            if m.get(key) is not None:
                cv.append(f"{label}: {m[key]:.3f}")
        if cv:
            blocks.extend(["<hr><b>Cross-validation</b><br>", "<br>".join(cv)])
        if m.get("y_scramble") is not None:
            label = "y-scramble Q²" if m.get("y_scramble_metric") == "q2_pooled_oof" else "y-scramble MCC"
            blocks.extend(["<hr>", f"<b>{label}:</b> {m['y_scramble']:.3f}"])
        warnings = [str(m[k]) for k in ("cv_warning", "y_scramble_warning") if m.get(k)]
        if warnings:
            blocks.extend(["<hr>", "<br>".join(warnings)])
        if n_selected > 1:
            blocks.extend(["<hr>", f"{n_selected} models selected: the charts overlay them; the molecules "
                                   f"listed are the first one's."])
        self.model_summary.setText("".join(blocks))

    # --- the members ----------------------------------------------------------
    def _load_members(self) -> None:
        """The molecules table of the first selected model, in the analysed subset. Rebuilt when
        either changes: its columns read that model's rows."""
        models = self._selected_models()
        subset = self.eval_subset.currentData() if hasattr(self, "eval_subset") else "test"
        key = (int(models[0].id), subset) if models else None
        if key == self._members_key and self.members_table is not None:
            return
        self._members_key = key
        if self.members_table is not None:
            self.members_table.setParent(None)
            self.members_table.deleteLater()
        config, clause = _members_config(self.runtime, *(key or (-1, "test")),
                                         empty=_empty_subset(models[0] if models else None, subset))
        self.members_table = project_table(self.runtime, config, self)
        self.members_table.set_external_clause("model", clause)
        self.members_table.selection_changed.connect(lambda picked: picked and self._on_member(picked[0]))
        self._members_host.addWidget(self.members_table)
        self._member_values = {}
        if key is not None:
            def _loaded(rows):
                if self._members_key == key:  # a later selection wins over a slower read
                    self._member_values = {r["molecule_id"]: r for r in rows}

            run_async(lambda: self.runtime.qsar.member_table(model=key[0]), _loaded, on_error=lambda _e: None)

    def _on_member(self, molecule) -> None:
        models = self._selected_models()
        member = self._member_values.get(int(molecule.id))
        if not models or member is None:
            return
        model = models[0]
        if model.task == "regression":
            cv = self.eval_subset.currentData() == "cv" and member["oof_predicted"] is not None
            highlight = getattr(self.window(), "highlight_fit_point", None)
            if highlight is not None and self._chart == "plot":
                highlight(member["observed"], member["oof_predicted"] if cv else member["predicted"])
        if (model.metrics or {}).get("feature_kind") != "ecfp4":
            return
        show = getattr(self.window(), "show_glowing_molecule", None)
        if show is None:
            return
        name = member["name"] or f"#{member['molecule_id']}"
        run_async(
            lambda: self.runtime.qsar.atom_contributions(model=int(model.id), ligand_id=member["molecule_id"]),
            lambda res: show(res["molblock"], res["weights"], f"{name}: pred {res['prediction']:.2f}"),
            on_error=lambda exc: self.status.setText(f"Glowing molecule: {exc}"),
        )

    # --- charts ---------------------------------------------------------------
    def _draw(self, kind: str, *, quiet: bool = False) -> None:
        """Draw one chart off the selection. ``quiet`` (a redraw on a selection change) skips a
        selection the chart cannot use instead of saying so."""
        models = self._selected_models()
        if not self._allowed(models)[kind]:
            if not quiet:
                QMessageBox.information(self, "QSAR", "Select one model." if kind in {"importance", "ad"}
                                        else "Select one or more models of the same task.")
            return
        self._chart = kind
        self._draws += 1
        {"plot": self._plot_fit, "importance": self._show_importance,
         "roc": self._show_roc, "ad": self._show_ad}[kind](models)

    def _latest(self, done):
        draw = self._draws
        return lambda *args: done(*args) if draw == self._draws else None

    def _redraw(self) -> None:
        """The chart again, off the current selection; the task's own chart if none was drawn."""
        dock = getattr(self.window(), "qsar_chart_dock", None)
        models = self._selected_models()
        if dock is None or not dock.isVisible() or not models:
            return
        self._draw(self._chart or ("roc" if models[0].task == "classification" else "plot"), quiet=True)

    restore_plot = _redraw  # the window calls it when the view comes back

    def _plot_fit(self, models) -> None:
        show = getattr(self.window(), "show_model_fit_series", None)
        if show is None:
            return
        specs = [(int(m.id), m.name) for m in models]
        subset = self.eval_subset.currentData()
        roles = ("train", "test") if subset == "all" else (subset,)

        def _compute():
            series = []
            for index, (mid, name) in enumerate(specs):
                for role in roles:
                    try:
                        res = self.runtime.qsar.observed_predicted_points(model=mid, subset=role)
                    except ValueError:  # no holdout, or no CV: that marker is just absent
                        continue
                    series.append({"label": f"#{mid} {name} · {role}", "points": res["points"], "ids": res["ids"],
                                   "color": index, "symbol": _SUBSET_SYMBOLS[role]})
            return series

        empty = _empty_subset(models[0], "test" if subset == "all" else subset)

        def _done(series):
            show(series, empty)
            self.status.setText(f"Observed vs predicted [{subset}]: "
                                + ", ".join(f"{s['label']} ({len(s['points'])})" for s in series)
                                if series else empty)

        run_async(_compute, self._latest(_done), on_error=lambda exc: QMessageBox.warning(self, "QSAR plot", str(exc)))

    def _show_importance(self, models) -> None:
        show = getattr(self.window(), "show_feature_importance", None)
        if show is None:
            return
        model = models[0]
        metrics = dict(model.metrics or {})
        pairs = [(str(n), float(v)) for n, v in metrics.get("feature_importance") or []][:15]
        if not pairs:
            self.status.setText("This model exposes no feature weights (tree and linear models do; kNN/SVM don't).")
            return
        label = f"#{model.id} {metrics.get('feature_kind', '')}"
        if metrics.get("feature_kind") != "ecfp4":
            show(label, pairs)
            return

        def _done(fragments: dict[str, str]):
            show(label, [(f"{n} · {fragments[n]}" if n in fragments else n, v) for n, v in pairs])

        run_async(lambda: self.runtime.qsar.bit_fragments(model=int(model.id), bits=[n for n, _v in pairs]),
                  self._latest(_done), on_error=self._latest(lambda _e: show(label, pairs)))

    def _show_roc(self, models) -> None:
        show = getattr(self.window(), "show_roc_curves", None)
        if show is None:
            return
        specs = [(int(m.id), m.name) for m in models]
        subset = self.eval_subset.currentData()

        def _compute():
            curves, failed = [], []
            for mid, name in specs:  # one bad model shouldn't sink the batch
                try:
                    res = self.runtime.qsar.roc_curve_points(model=mid, subset=subset)
                    curves.append({"label": f"#{mid} {name} (AUC={res['auc']:.3f}, n={res['n']})",
                                   "points": res["points"], "auc": res["auc"]})
                except Exception as exc:
                    failed.append(f"#{mid} {name}: {exc}")
            return curves, failed

        def _done(result):
            curves, failed = result
            show(curves, "No ROC curve: " + "; ".join(failed) if failed else "No ROC curve.")
            msg = f"ROC [{subset}]: " + ", ".join(c["label"] for c in curves) if curves else "No ROC curves produced."
            if failed:
                msg += " | Skipped: " + "; ".join(failed)
            self.status.setText(msg)

        run_async(_compute, self._latest(_done), on_error=lambda exc: QMessageBox.warning(self, "QSAR ROC", str(exc)))

    def _show_ad(self, models) -> None:
        show = getattr(self.window(), "show_similarity_distribution", None)
        if show is None:
            return
        model_id = int(models[0].id)

        def _work():
            from amdockvs.ui.tools.qsar.chart import histogram

            rows = self.runtime.qsar.applicability_domain(model=model_id)
            return histogram([r["similarity"] for r in rows]), sum(1 for r in rows if r["in_domain"]), len(rows)

        run_async(
            _work,
            self._latest(lambda res: (
                show(f"#{model_id} Tanimoto to training set", res[0]),
                self.status.setText(f"Applicability domain #{model_id}: {res[1]}/{res[2]} ligands in-domain "
                                    f"(mean Tanimoto ≥ {AD_THRESHOLD} to {AD_K} nearest training molecules)."))),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR applicability domain", str(exc)),
        )


class PredictionsWidget(QWidget):
    """Result analysis: per-model predictions as a filter; selecting a row drives the glowing-molecule dock."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.model_combo = QComboBox(self)
        self.model_combo.setMinimumWidth(180)
        self.model_combo.currentIndexChanged.connect(lambda _i: self._refresh_predictions())
        for w in (QLabel("Model:"), self.model_combo):
            toolbar.addWidget(w)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)
        outer.addLayout(self._cut_row())
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Ligand id", "Name", "Predicted", "Confidence", "AD"])
        self.table.horizontalHeaderItem(4).setToolTip(
            f"Applicability domain: mean Tanimoto to the {AD_K} nearest training molecules. "
            f"Below {AD_THRESHOLD} the prediction is an extrapolation.")
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select_row)
        outer.addWidget(self.table, 1)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self._fp_models: set[int] = set()  # only a fingerprint model maps a prediction back to atoms
        self.refresh()

    def _current_model_id(self) -> int | None:
        return self.model_combo.currentData()

    # --- the predictions as a filter -------------------------------------------------
    def _cut_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.cut_mode = QComboBox(self)
        for text, data in (("Top N highest", ("top", True)), ("Top N lowest", ("top", False)),
                           ("Predicted ≥", ("cutoff", True)), ("Predicted ≤", ("cutoff", False))):
            self.cut_mode.addItem(text, data)
        self.cut_mode.setToolTip("Which ligands this model's predictions keep: a number of the best, or a cutoff value.")
        self.cut_value = QDoubleSpinBox(self)
        self.cut_mode.currentIndexChanged.connect(self._sync_cut)
        self._sync_cut()
        self.cut_value.setValue(100)
        self.select_button = QPushButton("Use as selection", self)
        self.select_button.setToolTip(
            "Make the kept ligands the global ligand selection: every ligand table and tool works "
            "on them. Excludes nothing."
        )
        self.select_button.clicked.connect(self._use_as_selection)
        self.exclude_button = QPushButton("Exclude the rest", self)
        self.exclude_button.setToolTip(
            "Mark every other ligand this model scored as excluded. They stay in the project; "
            "reversible from Filter's 'Excluded' scope. Ligands the model could not score are left alone."
        )
        self.exclude_button.clicked.connect(self._exclude_rest)
        for w in (QLabel("Keep:"), self.cut_mode, self.cut_value, self.select_button, self.exclude_button):
            row.addWidget(w)
        row.addStretch(1)
        return row

    def _sync_cut(self, *_args) -> None:
        top = self.cut_mode.currentData()[0] == "top"
        self.cut_value.setDecimals(0 if top else 3)
        self.cut_value.setRange(1 if top else -1e9, 1e9)

    def _cut(self) -> dict:
        kind, higher = self.cut_mode.currentData()
        value = self.cut_value.value()
        return {"higher_is_better": higher, **({"top_n": int(value)} if kind == "top" else {"cutoff": float(value)})}

    def _run_cut(self, on_done, **how) -> None:
        model_id, cut = self._current_model_id(), self._cut()
        if model_id is None:
            QMessageBox.information(self, "QSAR", "Train or select a model first.")
            return
        run_async(
            lambda: self.runtime.qsar.cut_predictions(model=model_id, **cut, **how),
            on_done, on_error=lambda exc: QMessageBox.warning(self, "QSAR predictions", str(exc)), busy=self,
        )

    def _use_as_selection(self) -> None:
        def apply(result: dict) -> None:
            kept, store = result["kept"], getattr(self.window(), "selection", None)
            if store is None or not kept:
                self.status.setText("The cut keeps no ligand.")
            elif kept > _MAX_SELECTION:
                # ponytail: the global selection is a list of ids (see SelectionStore); a cut this
                # large stays a query only through 'Exclude the rest'.
                QMessageBox.information(
                    self, "QSAR predictions",
                    f"{kept} ligands is too many for a table selection (limit {_MAX_SELECTION}). "
                    f"Tighten the cut, or use 'Exclude the rest'.",
                )
            else:
                store.set("ligand", result["ids"])
                self.status.setText(f"{kept} ligand(s) selected; nothing was excluded.")

        self._run_cut(apply, ids_limit=_MAX_SELECTION)

    def _exclude_rest(self) -> None:
        def confirm(result: dict) -> None:
            kept, rest = result["kept"], result["rest"]
            if not rest:
                self.status.setText(f"Nothing to exclude: the cut keeps all {kept} scored ligand(s).")
                return
            if QMessageBox.question(
                self, "QSAR predictions",
                f"Keep {kept} ligand(s) and exclude the other {rest} scored by this model?\n\n"
                f"They stay in the project, marked excluded; reversible from Filter's 'Excluded' scope.",
            ) != QMessageBox.Yes:
                return
            self._run_cut(done, exclude_rest=True)

        def done(result: dict) -> None:
            self._refresh_predictions()
            self._cut_status = f"Kept {result['kept']} ligand(s), excluded {result['excluded']}."

        self._run_cut(confirm)

    def refresh(self) -> None:
        run_async(self.runtime.qsar.list_models, self._fill_models, on_error=lambda _e: None, busy=self.table)

    refresh_view = refresh

    def _fill_models(self, models) -> None:
        current = getattr(self, "_wanted_model", None) or self._current_model_id()
        self._wanted_model = None
        self._fp_models = {int(m.id) for m in models if (m.metrics or {}).get("feature_kind") == "ecfp4"}
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in models:
            self.model_combo.addItem(f"#{m.id} {m.name}", int(m.id))
        idx = self.model_combo.findData(current)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)
        self._refresh_predictions()

    def _refresh_predictions(self) -> None:
        model_id = self._current_model_id()
        if model_id is None:
            self.table.setRowCount(0)
            return
        run_async(
            lambda: self.runtime.qsar.prediction_rows(model=model_id),
            self._fill_predictions,
            on_error=lambda exc: self.status.setText(str(exc)),
            busy=self.table,
        )

    def _fill_predictions(self, rows) -> None:
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            conf = "" if row["confidence"] is None else f"{row['confidence']:.3f}"
            ad = row["ad_similarity"]
            ad_text = "" if ad is None else f"{ad:.2f}" + ("" if ad >= AD_THRESHOLD else " (out)")
            for c, val in enumerate([row["molecule_id"], row["name"], f"{row['value']:.4f}", conf, ad_text]):
                self.table.setItem(r, c, QTableWidgetItem(str(val)))
        glow = ("Select a row to glow the molecule." if self._current_model_id() in self._fp_models
                else "Glowing needs a fingerprint (ECFP4) model.")
        cut, self._cut_status = getattr(self, "_cut_status", ""), ""  # the outcome of a cut, shown once
        self.status.setText(f"{cut} {len(rows)} prediction(s). {glow}".strip()
                            if rows else "No predictions yet - run them from the QSAR tool's Predict tab.")

    def show_model(self, model_id: int) -> None:
        """Reload the models (a new one may have just predicted) and show this one's predictions."""
        self._wanted_model = model_id
        self.refresh()

    def _on_select_row(self) -> None:
        row = self.table.currentRow()
        model_id = self._current_model_id()
        if row < 0 or model_id is None or self.table.item(row, 0) is None:
            return
        if model_id not in self._fp_models:
            # A map left over from another model would read as this model's: take it down.
            hide = getattr(self.window(), "hide_glowing_molecule", None)
            if hide is not None:
                hide()
            return
        ligand_id = int(self.table.item(row, 0).text())
        name = self.table.item(row, 1).text()
        show = getattr(self.window(), "show_glowing_molecule", None)
        if show is None:
            return
        run_async(
            lambda: self.runtime.qsar.atom_contributions(model=model_id, ligand_id=ligand_id),
            lambda res: show(res["molblock"], res["weights"], f"{name}: pred {res['prediction']:.2f}"),
            on_error=lambda exc: self.status.setText(f"Glowing unavailable: {exc}"),
            busy=self.table,
        )


class QSARPredictWidget(QWidget):
    """The tool's Predict tab: score ligands or shards with the ticked models. Reading the
    predictions is the QSAR Analysis view's Result analysis tab."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self._models: list = []
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Tick the model(s) to predict with:", self))
        self.models_list = QListWidget(self)
        outer.addWidget(self.models_list, 1)
        row = QHBoxLayout()
        for text, tip, slot in (
            ("Predict all ligands", "Score every ligand with each ticked model.", self._predict),
            ("Predict shards", "Score the screening library in its shards with each ticked model, as a "
             "job. The scores stay by reference until 'Promote top…' turns the best into ligand rows.",
             self._predict_shards),
            ("Promote top…", "The ticked model's best shard scores become ligand rows, with their "
             "predictions (tick one; run 'Predict shards' first).", self._promote_shards),
        ):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        outer.addLayout(row)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    def showEvent(self, event) -> None:
        # ponytail: re-read on show, so a model trained in the Build tab is here without wiring.
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        run_async(self.runtime.qsar.list_models, self._fill_models, on_error=lambda _e: None,
                  busy=self.models_list)

    refresh_view = refresh

    def _fill_models(self, models) -> None:
        ticked = {int(m.id) for m in self._checked_models()}
        self._models = list(models)
        self.models_list.clear()
        for m in self._models:
            item = QListWidgetItem(f"#{m.id} {m.name} ({m.task}, {m.algorithm})", self.models_list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if int(m.id) in ticked else Qt.Unchecked)

    def _checked_models(self) -> list:
        return [m for r, m in enumerate(self._models)
                if r < self.models_list.count() and self.models_list.item(r).checkState() == Qt.Checked]

    def _require_checked(self, *, exactly_one: bool = False) -> list:
        checked = self._checked_models()
        if not checked or (exactly_one and len(checked) != 1):
            QMessageBox.information(self, "QSAR", "Tick exactly one model for this action." if exactly_one
                                    else "Tick at least one model to predict with.")
            return []
        return checked

    def _on_predicted(self, out) -> None:
        self.status.setText("Predicted - " + "; ".join(
            f"#{mid} {name}: {'done' if status == 'completed' else status}" for mid, name, status in out))
        done = [mid for mid, _name, status in out if status == "completed"]
        open_view = getattr(self.window(), "open_or_focus_view", None)
        if done and open_view is not None:
            open_view(QSAR_ANALYSIS_VIEW_ID).show_predictions(done[0])

    def _predict(self) -> None:
        checked = self._require_checked()
        if not checked:
            return
        specs = [(int(m.id), m.name) for m in checked]
        self.status.setText(f"Scoring the ligands with {len(specs)} model(s) as jobs - see Jobs.")

        def _work():  # one job per model; this thread only waits for them
            jobs = [(mid, name, self.runtime.qsar.submit_predict(model=mid)) for mid, name in specs]
            statuses = self.runtime.wait_for_jobs([job for *_x, job in jobs])
            return [(mid, name, statuses[job].status) for mid, name, job in jobs]

        run_async(_work, self._on_predicted, on_error=lambda exc: QMessageBox.warning(self, "QSAR predict", str(exc)))

    def _predict_shards(self) -> None:
        checked = self._require_checked()
        if not checked:
            return
        try:
            jobs = [self.runtime.qsar.predict_shards(model=int(m.id)) for m in checked]
        except Exception as exc:  # noqa: BLE001 — a project without shards, a missing model file
            QMessageBox.warning(self, "QSAR predict shards", str(exc))
            return
        self.status.setText(f"Scoring the shards: {len(jobs)} job(s) - see Jobs. Then 'Promote top…'.")

    def _promote_shards(self) -> None:
        checked = self._require_checked(exactly_one=True)
        if not checked:
            return
        model_id = int(checked[0].id)
        top_n, ok = QInputDialog.getInt(self, "Promote top", "Best-scored shard molecules to make ligand rows:",
                                        100, 1, 1_000_000, 10)
        if not ok:
            return
        higher = checked[0].task != "regression" or QMessageBox.question(
            self, "Promote top", "Is a higher predicted value better?") == QMessageBox.Yes
        run_async(
            lambda: self.runtime.qsar.promote_shard_predictions(model=model_id, top_n=int(top_n),
                                                                higher_is_better=higher),
            lambda res: self.status.setText(f"#{model_id}: {res['kept']} of {res['scored']} shard "
                                            f"molecules promoted to ligand rows, with their predictions."),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR promote", str(exc)),
            busy=self,
        )


def _lead_with(tabs: QTabWidget, widget: QWidget) -> None:
    """Move `widget`'s tab to the front and show it."""
    index = tabs.indexOf(widget)
    if index > 0:
        tabs.tabBar().moveTab(index, 0)
    tabs.setCurrentWidget(widget)


def _lead_when_models(tabs: QTabWidget, runtime, widget: QWidget) -> None:
    """A project with models leads with `widget` (using them); one without, with building them.
    A tab chosen before the models are read (a click, show_trained) keeps the focus."""
    tabs._chosen = False
    tabs.tabBarClicked.connect(lambda _i: setattr(tabs, "_chosen", True))
    run_async(runtime.qsar.list_models,
              lambda models: models and not tabs._chosen and _lead_with(tabs, widget),
              on_error=lambda _e: None)


class QSARToolWidget(QTabWidget):
    """The QSAR tool: Build | Predict."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.build = QSARBuildWidget(runtime=runtime, parent=self)
        self.predict = QSARPredictWidget(runtime=runtime, parent=self)
        self.addTab(self.build, "Build")
        self.addTab(self.predict, "Predict")
        _lead_when_models(self, runtime, self.predict)

    def refresh(self) -> None:
        self.build.refresh()
        self.predict.refresh()

    refresh_view = refresh


class QSARAnalysisWidget(QTabWidget):
    """The QSAR Analysis view: Model analysis | Result analysis."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.models = QSARModelsWidget(runtime=runtime, parent=self)
        self.results = PredictionsWidget(runtime=runtime, parent=self)
        self.addTab(self.models, "Model analysis")
        self.addTab(self.results, "Result analysis")
        _lead_when_models(self, runtime, self.results)

    def refresh(self) -> None:
        self.models.refresh()
        self.results.refresh()

    refresh_view = refresh

    def restore_plot(self) -> None:
        self.models.restore_plot()

    def show_trained(self, request: dict) -> None:
        self._chosen = True
        self.setCurrentWidget(self.models)
        self.models.show_trained(request)

    def show_predictions(self, model_id: int) -> None:
        self._chosen = True
        self.setCurrentWidget(self.results)
        self.results.show_model(model_id)


def register_qsar_panels(window) -> None:
    window.register_main_view(
        QSAR_BUILD_VIEW_ID, "QSAR",
        lambda: QSARToolWidget(runtime=window.runtime, parent=window.central_widget),
    )
    window.register_main_view(
        QSAR_ANALYSIS_VIEW_ID, "QSAR Analysis",
        lambda: QSARAnalysisWidget(runtime=window.runtime, parent=window.central_widget),
    )
    # Where each one shows up (left tool bar vs. top catalog bar) is decided in ui/registry.py.


__all__ = ["PredictionsWidget", "QSARAnalysisWidget", "QSARBuildWidget", "QSARModelsWidget",
           "QSARPredictWidget", "QSARToolWidget", "register_qsar_panels"]
