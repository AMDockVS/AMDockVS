"""QSAR workspace panels over the lean runtime.qsar API.

Thin Qt over the script-mode APIs (which already work standalone): edit activities, compute
descriptors, train/predict/evaluate model templates, and run high-throughput pre-docking
filtering. Heavy calls go through run_async so the GUI never blocks.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.qsar.modeling import supported_algorithms
from amdockvs.ui.common.async_query import run_async
from ms_components.ms_stepper import Orientation, QStepper
from ms_components.chips import FlowLayout
from ms_components.tool_panel import ToolPanel
from amdockvs.ui.view_ids import PREDICTIONS_VIEW_ID, QSAR_BUILD_VIEW_ID, QSAR_MODELS_VIEW_ID


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


def _form_step(title: str, rows: list[tuple[str, QWidget]]) -> ToolPanel:
    """Use the same section spacing and scrolling as Docking Studio's steps."""
    page = ToolPanel()
    section = page.add_section(title)
    for label, widget in rows:
        if label:
            section.add_row(label, widget)
        else:
            section.add_row(widget)
    return page


def _metric_display_name(key: str) -> str:
    """Readable QSAR metric labels with conventional capitalization for abbreviations."""
    known = {
        "accuracy": "Accuracy",
        "balanced_accuracy": "Balanced accuracy",
        "f1": "F1",
        "mcc": "MCC",
        "roc_auc": "ROC AUC",
        "pr_auc": "PR AUC",
        "r2": "R²",
        "rmse": "RMSE",
        "mae": "MAE",
        "mse": "MSE",
        "enrichment_1pct": "Enrichment at 1%",
        "cv_mcc": "CV MCC",
        "cv_accuracy": "CV accuracy",
    }
    return known.get(key, key.replace("_", " ").capitalize())


# ---------------------------------------------------------------------------
# Build model (left-bar tool)
# ---------------------------------------------------------------------------

class QSARBuildWidget(QWidget):
    """The build-a-model wizard. It only builds: what it trains is read in Model Analysis."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self._train_box(), 1)
        outer.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        run_async(self.runtime.qsar.endpoint_kinds, self._fill_endpoints, on_error=lambda _e: None,
                  busy=self.stepper)

    refresh_view = refresh

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
            show(f"{endpoint} — {split} split (train vs test)", res["categories"],
                 {"train": res["train"], "test": res["test"]})
            self.status.setText(f"Split preview [{split}] {endpoint}: "
                                f"{res['n_train']} train / {res['n_test']} test — {res['note']}")

        run_async(
            lambda: self.runtime.qsar.split_preview(
                endpoint=endpoint, split=split, test_size=test_size, class_threshold=threshold),
            _done,
            on_error=lambda exc: QMessageBox.warning(self, "QSAR split preview", str(exc)),
            busy=self,
        )

    def _compute_descriptors(self) -> None:
        try:
            job_id = self.runtime.qsar.compute_descriptors(
                only_missing=True, compute_fingerprints=self.morgan_check.isChecked()
            )
        except Exception as exc:
            QMessageBox.warning(self, "QSAR", str(exc))
            return
        self.status.setText(f"Descriptor job submitted: {job_id}")

    # --- training -------------------------------------------------------------
    def _train_box(self) -> QWidget:
        """Build-a-model wizard: a QStepper (same component the docking studio uses) over the
        same train controls, laid out as StarDrop's Auto-Modeler steps."""
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self.train_endpoint = QComboBox(box)
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
        self.split_combo.addItems(["random", "scaffold"])
        self.split_combo.setToolTip("Scaffold split holds out whole Bemis-Murcko scaffolds (realistic QSAR test).")
        self.test_size = QDoubleSpinBox(box)
        self.test_size.setRange(0.0, 0.9)
        self.test_size.setSingleStep(0.1)
        self.test_size.setValue(0.3)
        split_preview_btn = QPushButton("Preview split distribution", box)
        split_preview_btn.setToolTip("Class/value distribution in train vs test — scaffold split balances "
                                     "chemical space but NOT the labels, so check the actives aren't lopsided.")
        split_preview_btn.clicked.connect(self._preview_split)
        self.feature_source = QComboBox(box)
        self.feature_source.addItems(["descriptors", "rdkit2d", "ecfp4"])
        self.feature_source.setToolTip(
            "descriptors: 14 physchem columns · rdkit2d: full RDKit ~200 block (computed+cached on "
            "first use) · ecfp4: stored Morgan fingerprint. Descriptor blocks are auto-scaled and "
            "correlation-pruned inside the CV pipeline.")
        self.corr_threshold = QDoubleSpinBox(box)
        self.corr_threshold.setRange(0.5, 1.0)
        self.corr_threshold.setSingleStep(0.05)
        self.corr_threshold.setValue(0.95)
        self.corr_threshold.setToolTip("Drop one of every descriptor pair with |r| above this (ignored for ecfp4).")
        self.y_scramble = QSpinBox(box)
        self.y_scramble.setRange(0, 50)
        self.y_scramble.setToolTip("y-randomisation: refit on N shuffled-label sets (CV-scored). "
                                   "Should score ~0; if near the real score, the model is chance. 0 = off.")
        self.morgan_check = QCheckBox("Morgan fingerprints (ECFP4)", box)
        self.morgan_check.setToolTip("Also compute/store the Morgan fingerprint (needed for the ECFP4 feature source).")
        step3_desc_btn = QPushButton("Compute missing descriptors", box)
        step3_desc_btn.setToolTip("Skips ligands that already have them.")
        step3_desc_btn.clicked.connect(self._compute_descriptors)
        corr_btn = QPushButton("Analyze correlation…", box)
        corr_btn.setToolTip("Heatmap of THIS feature block (above) at THIS cutoff — shows which "
                            "descriptors the pipeline prunes as redundant before training.")
        corr_btn.clicked.connect(self._show_correlation)
        self.algo_box = QWidget(box)
        self.algo_box_layout = QVBoxLayout(self.algo_box)
        self.algo_box_layout.setContentsMargins(0, 0, 0, 0)
        self.algo_checks: dict[str, QCheckBox] = {}
        self.cv_folds = QSpinBox(box)
        self.cv_folds.setRange(0, 20)
        self.cv_folds.setToolTip(
            "k-fold cross-validation within the training set (0 = off). Regression Q² uses pooled "
            "out-of-fold predictions: 1 − PRESS / training response sum of squares. "
            "With a scaffold split, whole scaffolds stay together in each CV fold.")

        # Numbered headers navigate freely, as in Docking Studio. The final "Train" button
        # remains the explicit action that starts training (finished signal).
        self.stepper = QStepper(orientation=Orientation.HORIZONTAL, linear=False,
                                alternative_labels=True, show_navigation=True, parent=box)
        # Use the application's standard buttons, matching Docking Studio's actions,
        # rather than the stepper's filled-primary and borderless button styles.
        self.stepper.btn_back.setObjectName("qsarBack")
        self.stepper.btn_next.setObjectName("qsarNext")
        self.stepper.set_qss("QFrame#stepperNav { border-top: 1px solid palette(mid); }")
        self.stepper.finish_text = "Train"
        self.stepper.finished.connect(self._train)
        self.stepper.add_step("Data", "endpoint & task").add_widget(
            _form_step("Training data", [("Endpoint", self.train_endpoint), ("Task", self.task_combo),
                        ("Class threshold", self.class_threshold)]))
        self.stepper.add_step("Split", "train/test").add_widget(
            _form_step("Train/test split", [("Split", self.split_combo), ("Test split", self.test_size), ("", split_preview_btn)]))
        self.stepper.add_step("Descriptors", "features").add_widget(
            _form_step("Descriptors", [("Features", self.feature_source), ("Correlation cutoff", self.corr_threshold),
                        ("", corr_btn), ("", self.morgan_check), ("", step3_desc_btn)]))
        self.stepper.add_step("Methods", "algorithms & run").add_widget(
            _form_step("Training methods", [("Algorithms", self.algo_box), ("CV folds", self.cv_folds),
                        ("y-scramble runs", self.y_scramble)]))
        for step in self.stepper.steps():
            step.body_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stepper)
        self._sync_algorithms()
        return box

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
        self.stepper.setEnabled(False)
        self.status.setText(f"Training {len(algos)} model(s)…")

        def _work():
            out = []
            for algo in algos:
                try:
                    m = self.runtime.qsar.train(algorithm=algo, **base)
                    out.append(("ok", {"id": int(m.id), "name": m.name, "algorithm": m.algorithm,
                                       "metrics": dict(m.metrics or {})}))
                except Exception as exc:  # one bad algorithm shouldn't sink the batch
                    out.append(("err", f"{algo}: {exc}"))
            return out

        run_async(_work, self._on_trained_batch, on_error=self._on_train_error, busy=self)

    def _on_trained_batch(self, results) -> None:
        self.stepper.setEnabled(True)
        trained = [r for status, r in results if status == "ok"]
        errors = [r for status, r in results if status == "err"]
        open_view = getattr(self.window(), "open_or_focus_view", None)
        if trained and open_view is not None:
            open_view(QSAR_MODELS_VIEW_ID).show_trained(trained)
        msg = f"Trained {len(trained)} model(s); refreshed Model Analysis."
        if errors:
            msg += " Failed: " + "; ".join(errors)
        self.status.setText(msg)

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
            on_error=lambda exc: QMessageBox.warning(self, "QSAR correlation", str(exc)),
            busy=self,
        )

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

class QSARModelsWidget(QWidget):
    """Review trained QSAR models and inspect their diagnostics."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(_scrolled(self._models_box()), 1)
        outer.addWidget(self.status)
        self.refresh()

    def show_trained(self, rows: list[dict]) -> None:
        """Called by the Build Model tool after a training run."""
        self.refresh()

    # --- models ---------------------------------------------------------------
    def _models_box(self) -> QGroupBox:
        box = QGroupBox(self)
        layout = QVBoxLayout(box)
        self.models_table = QTableWidget(0, 8, box)
        self.models_table.setHorizontalHeaderLabels(
            ["Model", "Task", "Algorithm", "Features", "Endpoint", "Split", "Train n", "Test n"])
        self.models_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.models_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.models_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.models_table.setAlternatingRowColors(True)
        header = self.models_table.horizontalHeader()
        for column in (0, 1, 2, 3, 5, 6, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.models_table.setMaximumHeight(205)
        self.models_table.itemSelectionChanged.connect(self._show_model_summary)
        layout.addWidget(self.models_table)
        self.model_summary = QLabel("Select a model row to review its results.", box)
        self.model_summary.setWordWrap(True)
        self.model_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.model_summary.setMinimumHeight(96)
        self.model_summary.setStyleSheet("QLabel { padding: 8px; border: 1px solid palette(mid); border-radius: 4px; }")
        layout.addWidget(self.model_summary)

        # Model comparison and use actions are temporarily omitted while their workflow is redesigned.
        self._action_buttons: dict[str, QPushButton] = {}

        def _btn(key: str, text: str, tip: str, slot) -> QPushButton:
            b = QPushButton(text, box)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            self._action_buttons[key] = b
            return b

        # ROC / observed-vs-predicted can be scored on the held-out test (honest predictivity), the
        # train set (its gap to test = overfitting), or all labeled. Test is the default.
        self.eval_subset = QComboBox(box)
        for text, data in (("Test", "test"), ("Train", "train"), ("All labeled", "all")):
            self.eval_subset.addItem(text, data)
        self.eval_subset.setToolTip("Which ligands evaluation and diagnostic plots score on. Test = honest "
                                    "predictivity; Train vs Test gap = overfitting; All = everything labeled.")

        analyze_actions = QWidget(box)
        analyze_actions.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        analyze_row = FlowLayout(analyze_actions, spacing=layout.spacing())
        analyze_row.addWidget(QLabel("Analyze:", box))
        analyze_row.addWidget(QLabel("on:", box))
        analyze_row.addWidget(self.eval_subset)
        analyze_row.addWidget(_btn("importance", "Feature importance",
                                   "Bar chart of the selected model's feature weights: tree importances or "
                                   "linear coefficients (select exactly one row).",
                                   self._show_importance))
        analyze_row.addWidget(
            _btn("roc", "ROC curve", "ROC + AUC; selected models overlay (classification).", self._show_roc))
        analyze_row.addWidget(_btn("pr", "Precision-recall", "Binary probabilities, average precision (AP), "
                                   "and positive prevalence on the selected subset.", self._show_pr))
        analyze_row.addWidget(_btn("confusion", "Confusion matrix", "Actual vs predicted class counts "
                                   "on the selected subset (select one binary classifier row).", self._show_confusion))
        analyze_row.addWidget(
            _btn("ad", "Applicability domain", "Tanimoto to the training set (select one; can be slow for big sets).",
                 self._show_ad))
        analyze_row.addWidget(
            _btn("glowing", "Glowing molecule…", "Per-atom contribution - ECFP4 models only (select one row).",
                 self._glowing_molecule))
        layout.addSpacing(8)
        layout.addWidget(analyze_actions)
        self._update_action_state()
        return box

    # --- action targeting (the selected model rows drive both details and actions) ---
    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.models_table.selectionModel().selectedRows()})

    def _selected_models(self) -> list:
        models = getattr(self, "_models", [])
        return [models[r] for r in self._selected_rows() if r < len(models)]

    def _update_action_state(self) -> None:
        """Enable actions according to the selected model rows and their task/features."""
        if not getattr(self, "_action_buttons", None):
            return
        selected = self._selected_models()
        n = len(selected)
        kinds = {str((m.metrics or {}).get("feature_kind", "")) for m in selected}
        tasks = {str((m.metrics or {}).get("task", "")) for m in selected}
        one_ecfp4 = n == 1 and kinds == {"ecfp4"}
        all_classification = n >= 1 and tasks == {"classification"}
        rules = {
            "roc": all_classification,  # ROC is classification-only
            "pr": all_classification, "confusion": n == 1 and all_classification,
            "importance": n == 1, "ad": n == 1, "glowing": one_ecfp4,
        }
        for key, enabled in rules.items():
            self._action_buttons[key].setEnabled(bool(enabled))
        self._action_buttons["roc"].setToolTip(
            "ROC + AUC — classification models only." if not all_classification
            else "ROC + AUC over the labeled ligands; selected models overlay.")
        self._action_buttons["glowing"].setToolTip(
            "Per-atom contribution - ECFP4 models only (select exactly one ECFP4 model)."
            if not one_ecfp4 else "Per-atom contribution of a ligand to this ECFP4 model.")

    def _require_selected(self, *, exactly_one: bool = False) -> list:
        selected = self._selected_models()
        if not selected:
            QMessageBox.information(self, "QSAR", "Select at least one model row to act on.")
            return []
        if exactly_one and len(selected) != 1:
            QMessageBox.information(self, "QSAR", "Select exactly one model row for this action.")
            return []
        return selected

    def _plot_fit(self) -> None:
        checked = self._require_selected()
        if not checked:
            return
        if any(str((m.metrics or {}).get("task", "")) != "regression" for m in checked):
            QMessageBox.information(self, "QSAR", "Observed-vs-predicted is a regression diagnostic. For "
                                                  "classification use ROC curve / Evaluate instead.")
            return
        show = getattr(self.window(), "show_model_fit_series", None)
        if show is None:
            return
        model_specs = [(int(m.id), m.name) for m in checked]
        subset = self.eval_subset.currentData()

        def _compute():
            series = []
            for mid, name in model_specs:
                result = self.runtime.qsar.observed_predicted_points(model=mid, subset=subset)
                series.append({"label": f"{name} [{subset}]", "points": result["points"]})
            return series

        run_async(
            _compute,
            lambda series: (show(series), self.status.setText(
                f"Observed vs predicted [{subset}]: "
                + ", ".join(f"{s['label']} ({len(s['points'])})" for s in series))),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR plot", str(exc)),
            busy=self,
        )

    def _show_importance(self) -> None:
        checked = self._require_selected(exactly_one=True)
        if not checked:
            return
        metrics = dict(checked[0].metrics or {})
        pairs = metrics.get("feature_importance") or []
        if not pairs:
            QMessageBox.information(self, "QSAR", "This model exposes no feature weights (tree and linear "
                                                  "models do; kNN/SVM don't). Models trained before "
                                                  "linear weights were stored need a retrain.")
            return
        show = getattr(self.window(), "show_feature_importance", None)
        if show is not None:
            show(f"#{checked[0].id} {metrics.get('feature_kind', '')}", [(str(n), float(v)) for n, v in pairs])
            self.status.setText(f"Top {min(len(pairs), 15)} features of #{checked[0].id}.")

    def _show_roc(self) -> None:
        self._show_classification_curves("roc")

    def _show_pr(self) -> None:
        self._show_classification_curves("pr")

    def _show_classification_curves(self, kind: str) -> None:
        checked = self._require_selected()
        if not checked:
            return
        show = getattr(self.window(), "show_roc_curves" if kind == "roc" else "show_precision_recall_curves", None)
        if show is None:
            return
        specs = [(int(m.id), m.name) for m in checked]
        subset = self.eval_subset.currentData()
        title = "ROC" if kind == "roc" else "Precision-recall"

        def _compute():
            curves, failed = [], []
            for mid, name in specs:  # one bad model shouldn't sink the batch
                try:
                    method = (self.runtime.qsar.roc_curve_points if kind == "roc"
                              else self.runtime.qsar.precision_recall_curve_points)
                    res = method(model=mid, subset=subset)
                    score = f"AUC={res['auc']:.3f}" if kind == "roc" else f"AP={res['average_precision']:.3f}"
                    curves.append({**res, "label": f"{name} [{subset}] ({score}, n={res['n']})"})
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
            return curves, failed

        def _done(result):
            curves, failed = result
            if curves:
                show(curves)
            msg = f"{title} [{subset}]: " + ", ".join(c["label"] for c in curves) if curves else f"No {title} curves produced."
            if failed:
                msg += " | Skipped: " + "; ".join(failed)
            self.status.setText(msg)

        run_async(_compute, _done, on_error=lambda exc: QMessageBox.warning(self, f"QSAR {title}", str(exc)), busy=self)

    def _show_confusion(self) -> None:
        checked = self._require_selected(exactly_one=True)
        if not checked:
            return
        model_id, name = int(checked[0].id), checked[0].name
        subset = self.eval_subset.currentData()
        show = getattr(self.window(), "show_confusion_matrix", None)
        if show is None:
            return

        def _done(result):
            label = f"#{model_id} {name} [{subset}], n={result['n']}"
            show(label, result)
            (tn, fp), (fn, tp) = result["counts"]
            self.status.setText(f"Confusion matrix [{subset}]: n={result['n']}, "
                                f"TN={tn}, FP={fp}, FN={fn}, TP={tp}; positive class={result['positive_class']}")

        run_async(lambda: self.runtime.qsar.confusion_matrix_data(model=model_id, subset=subset), _done,
                  on_error=lambda exc: QMessageBox.warning(self, "QSAR confusion matrix", str(exc)), busy=self)

    def _show_ad(self) -> None:
        checked = self._require_selected(exactly_one=True)
        if not checked:
            return
        model_id = int(checked[0].id)
        show = getattr(self.window(), "show_similarity_distribution", None)
        if show is None:
            return
        self.status.setText("Computing applicability domain… (Tanimoto to training set)")

        def _work():
            from amdockvs.ui.tools.qsar.chart import histogram

            rows = self.runtime.qsar.applicability_domain(model=model_id)
            sims = [r["similarity"] for r in rows]
            in_domain = sum(1 for r in rows if r["in_domain"])
            return histogram(sims), in_domain, len(rows)

        run_async(
            _work,
            lambda res: (show(f"#{model_id} Tanimoto to training set", res[0]),
                         self.status.setText(f"Applicability domain #{model_id}: {res[1]}/{res[2]} ligands in-domain "
                                             f"(mean Tanimoto ≥ 0.3 to 5 nearest training molecules).")),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR applicability domain", str(exc)),
            busy=self,
        )

    def _glowing_molecule(self) -> None:
        from amdockvs.ui.tools.qsar.activities import pick_ligands

        checked = self._require_selected(exactly_one=True)
        if not checked:
            return
        model_id = int(checked[0].id)
        chosen = pick_ligands(self, self.runtime)
        if not chosen:
            return
        ligand_id, name = chosen[0]
        show = getattr(self.window(), "show_glowing_molecule", None)
        if show is None:
            return
        run_async(
            lambda: self.runtime.qsar.atom_contributions(model=model_id, ligand_id=ligand_id),
            lambda res: (show(res["molblock"], res["weights"], f"{name}: pred {res['prediction']:.2f}"),
                         self.status.setText(f"Glowing molecule for {name} (pred {res['prediction']:.2f}).")),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR glowing", str(exc)),
            busy=self,
        )

    def _predict(self) -> None:
        checked = self._require_selected()
        if not checked:
            return
        specs = [(int(m.id), m.name) for m in checked]

        def _work():
            return [(mid, name, self.runtime.qsar.predict(model=mid)) for mid, name in specs]

        run_async(
            _work,
            lambda out: self.status.setText("Predicted — " + "; ".join(
                f"#{mid} {name}: {res['predicted']} scored ({res['skipped_missing_descriptor']} skipped)"
                for mid, name, res in out)),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR predict", str(exc)),
            busy=self,
        )

    def _predict_shards(self) -> None:
        checked = self._require_selected()
        if not checked:
            return
        try:
            jobs = [self.runtime.qsar.predict_shards(model=int(m.id)) for m in checked]
        except Exception as exc:  # noqa: BLE001 — a project without shards, a missing model file
            QMessageBox.warning(self, "QSAR predict shards", str(exc))
            return
        self.status.setText(f"Scoring the shards: {len(jobs)} job(s) — see Jobs. Then 'Promote top…'.")

    def _promote_shards(self) -> None:
        checked = self._require_selected(exactly_one=True)
        if not checked:
            return
        model_id = int(checked[0].id)
        top_n, ok = QInputDialog.getInt(self, "Promote top", "Best-scored shard molecules to make ligand rows:",
                                        100, 1, 1_000_000, 10)
        if not ok:
            return
        higher = str((checked[0].metrics or {}).get("task", "")) != "regression" or QMessageBox.question(
            self, "Promote top", "Is a higher predicted value better?") == QMessageBox.Yes
        run_async(
            lambda: self.runtime.qsar.promote_shard_predictions(model=model_id, top_n=int(top_n),
                                                                higher_is_better=higher),
            lambda res: self.status.setText(f"#{model_id}: {res['kept']} of {res['scored']} shard "
                                            f"molecules promoted to ligand rows, with their predictions."),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR promote", str(exc)),
            busy=self,
        )

    def _evaluate(self) -> None:
        checked = self._require_selected()
        if not checked:
            return
        specs = [(int(m.id), m.name) for m in checked]
        subset = self.eval_subset.currentData()

        def _work():
            return [(mid, name, self.runtime.qsar.evaluate(model=mid, subset=subset)) for mid, name in specs]

        def _fmt(res: dict) -> str:
            keys = ("mcc", "roc_auc", "r2", "rmse")
            return ", ".join(f"{k}={res[k]:.3f}" for k in keys if isinstance(res.get(k), float))

        run_async(
            _work,
            lambda out: self.status.setText(f"Evaluate [{subset}] - " + "; ".join(
                f"#{mid} {name} (n={res['n_samples']}): {_fmt(res)}" for mid, name, res in out)),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR evaluate", str(exc)),
            busy=self,
        )

    def refresh(self) -> None:
        run_async(self.runtime.qsar.list_models, self._fill_models, on_error=lambda _e: None, busy=self.models_table)

    refresh_view = refresh

    def _fill_models(self, models) -> None:
        self._models = list(models)
        self.models_table.blockSignals(True)
        self.models_table.setRowCount(len(models))
        for r, m in enumerate(models):
            metrics = m.metrics or {}
            task = str(metrics.get("task") or "unknown").replace("_", " ").title()
            feature_kind = str(metrics.get("feature_kind") or "—").upper()
            train_n = metrics.get("n_train", "—")
            test_n = (metrics.get("test") or {}).get("n_samples", "—")
            values = [m.name, task, m.algorithm, feature_kind, m.target,
                      str(metrics.get("split") or "—").replace("_", " ").title(), train_n, test_n]
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                if c in (6, 7):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:
                    item.setToolTip(m.name)
                self.models_table.setItem(r, c, item)
        self.models_table.blockSignals(False)
        if models and self.models_table.currentRow() < 0:
            self.models_table.selectRow(0)
        elif not models:
            self.model_summary.setText("No trained QSAR models are stored in this project yet. Build a model in QSAR Build.")
        self._update_action_state()

    def _show_model_summary(self) -> None:
        self._update_action_state()
        selected_rows = self._selected_rows()
        row = selected_rows[0] if selected_rows else -1
        models = getattr(self, "_models", [])
        if row < 0 or row >= len(models):
            self.model_summary.setText("Select a model row to review its results.")
            return
        record = models[row]
        m = record.metrics or {}

        def metric_lines(values):
            return "<br>".join(
                f"<b>{_metric_display_name(key)}</b> {value:.3f}"
                if isinstance(value, (float, int)) and not isinstance(value, bool)
                else f"<b>{_metric_display_name(key)}</b> {value}"
                for key, value in (values or {}).items() if key != "n_samples" and value is not None
            ) or "—"

        task = str(m.get("task", "unknown")).replace("_", " ").title()
        header = (f"<b>{record.name}</b> &nbsp; · &nbsp; {task} &nbsp; · &nbsp; "
                  f"{record.algorithm} &nbsp; · &nbsp; {str(m.get('feature_kind', '—')).upper()}"
                  f"<br><span>Endpoint: {record.target} &nbsp; · &nbsp; Split: {m.get('split', '—')} &nbsp; · &nbsp; "
                  f"Training samples: {m.get('n_train', '—')}</span>")
        blocks = [header, "<hr>"]
        if m.get("train") or m.get("test"):
            train_n = (m.get("train") or {}).get("n_samples", m.get("n_train", "—"))
            test_n = (m.get("test") or {}).get("n_samples", "—")
            blocks.append(
                "<table width='100%'><tr>"
                f"<th align='left'>Training (n={train_n})</th><th align='left'>Holdout test (n={test_n})</th>"
                "</tr><tr>"
                f"<td valign='top'>{metric_lines(m.get('train'))}</td>"
                f"<td valign='top'>{metric_lines(m.get('test'))}</td>"
                "</tr></table>")
        cv_parts = []
        if "cv_samples" in m:
            cv_parts.append(f"Training-only cross-validation · n={m['cv_samples']} · {m.get('cv_folds', '—')} folds · "
                            f"{m.get('cv_split', '—')} split")
        if m.get("q2_method") == "pooled_oof":
            if m.get("q2") is not None:
                cv_parts.append(f"Pooled out-of-fold Q²: {m['q2']:.3f}")
            cv_parts.append(f"Mean fold R²: {m['cv_r2_mean']:.3f}" if m.get("cv_r2_mean") is not None
                            else "Mean fold R² undefined")
            if m.get("cv_rmse") is not None:
                cv_parts.append(f"CV RMSE: {m['cv_rmse']:.3f}")
        elif m.get("q2") is not None:
            cv_parts.append(f"Mean fold R² (legacy Q²): {m['q2']:.3f}; retrain to calculate pooled Q².")
        for key in ("cv_mcc", "cv_accuracy"):
            if m.get(key) is not None:
                cv_parts.append(f"{_metric_display_name(key)}: {m[key]:.3f}")
        if cv_parts:
            blocks.extend(["<hr><b>Cross-validation (training data only)</b><br>", "<br>".join(cv_parts)])
        if m.get("y_scramble") is not None:
            name = ("y-scramble Q² (pooled out-of-fold)" if m.get("y_scramble_metric") == "q2_pooled_oof"
                    else "y-scramble MCC" if m.get("task") == "classification"
                    else "y-scramble mean fold R² (legacy)")
            blocks.extend(["<hr>", f"<b>{name}:</b> {m['y_scramble']:.3f}"])
        warnings = []
        for key in ("cv_warning", "y_scramble_warning"):
            if m.get(key):
                warnings.append(str(m[key]))
        if warnings:
            blocks.extend(["<hr>", "<br>".join(warnings)])
        if len(selected_rows) > 1:
            blocks.extend(["<hr>", f"{len(selected_rows)} model rows selected; actions apply to this selection."])
        self.model_summary.setText("".join(blocks))


class PredictionsWidget(QWidget):
    """Per-model predictions table; selecting a row drives the glowing-molecule dock."""

    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to view predictions.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return
        toolbar = QHBoxLayout()
        self.model_combo = QComboBox(self)
        self.model_combo.setMinimumWidth(180)
        self.model_combo.currentIndexChanged.connect(lambda _i: self._refresh_predictions())
        run_btn = QPushButton("Run prediction (all ligands)", self)
        run_btn.clicked.connect(self._run)
        for w in (QLabel("Model:"), self.model_combo, run_btn):
            toolbar.addWidget(w)
        toolbar.addStretch(1)
        outer.addLayout(toolbar)
        outer.addLayout(self._cut_row())
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Ligand", "Prediction", "Confidence", "P(positive class)", "ID"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 300)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
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
        self.cut_mode.setToolTip(
            "Keep the best N or apply a cutoff. Binary classifiers use P(the higher-valued class); "
            "regressors use the predicted value.")
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
        current = self._current_model_id()
        self._fp_models = {int(m.id) for m in models if (m.metrics or {}).get("feature_kind") == "ecfp4"}
        self._classification_models = {int(m.id): (m.metrics or {}).get("positive_class")
                                       for m in models if (m.metrics or {}).get("task") == "classification"}
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
        classifiers = getattr(self, "_classification_models", {})
        classification = model_id in classifiers
        positive_class = classifiers.get(model_id)
        probability_label = f"P(class {positive_class})" if positive_class is not None else "P(positive class)"
        self.table.setColumnHidden(3, not classification)
        self.table.setHorizontalHeaderItem(3, QTableWidgetItem(probability_label))
        self.cut_mode.setItemText(2, f"{probability_label if classification else 'Predicted'} ≥")
        self.cut_mode.setItemText(3, f"{probability_label if classification else 'Predicted'} ≤")
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
        classification = self._current_model_id() in getattr(self, "_classification_models", {})
        for r, row in enumerate(rows):
            conf = "" if row["confidence"] is None else f"{row['confidence']:.3f}"
            probability = row.get("positive_probability")
            probability_text = "" if probability is None else f"{probability:.4f}"
            prediction = f"{float(row['value']):g}" if classification else f"{row['value']:.4f}"
            values = [row["name"], prediction, conf, probability_text, row["molecule_id"]]
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                if c in (1, 2, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    item.setToolTip(str(val))
                self.table.setItem(r, c, item)
        glow = ("Select a row to glow the molecule." if self._current_model_id() in self._fp_models
                else "Glowing needs a fingerprint (ECFP4) model.")
        cut, self._cut_status = getattr(self, "_cut_status", ""), ""  # the outcome of a cut, shown once
        self.status.setText(f"{cut} {len(rows)} prediction(s). {glow}".strip()
                            if rows else "No predictions yet — click 'Run prediction'.")

    def _run(self) -> None:
        model_id = self._current_model_id()
        if model_id is None:
            QMessageBox.information(self, "QSAR", "Train or select a model first.")
            return
        run_async(
            lambda: self.runtime.qsar.predict(model=model_id),
            lambda res: (self.status.setText(f"Predicted {res['predicted']} ligand(s)."), self._refresh_predictions()),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR predict", str(exc)),
            busy=self,
        )

    def _on_select_row(self) -> None:
        row = self.table.currentRow()
        model_id = self._current_model_id()
        if row < 0 or model_id is None or self.table.item(row, 4) is None:
            return
        if model_id not in self._fp_models:
            # A map left over from another model would read as this model's: take it down.
            hide = getattr(self.window(), "hide_glowing_molecule", None)
            if hide is not None:
                hide()
            return
        ligand_id = int(self.table.item(row, 4).text())
        name = self.table.item(row, 0).text()
        show = getattr(self.window(), "show_glowing_molecule", None)
        if show is None:
            return
        run_async(
            lambda: self.runtime.qsar.atom_contributions(model=model_id, ligand_id=ligand_id),
            lambda res: show(res["molblock"], res["weights"], f"{name}: pred {res['prediction']:.2f}"),
            on_error=lambda exc: self.status.setText(f"Glowing unavailable: {exc}"),
            busy=self.table,
        )


def register_qsar_panels(window) -> None:
    window.register_main_view(
        QSAR_BUILD_VIEW_ID, "Build Model",
        lambda: QSARBuildWidget(runtime=window.runtime, parent=window.central_widget),
    )
    window.register_main_view(
        QSAR_MODELS_VIEW_ID, "Model Analysis",
        lambda: QSARModelsWidget(runtime=window.runtime, parent=window.central_widget),
    )
    window.register_main_view(
        PREDICTIONS_VIEW_ID, "Predictions",
        lambda: PredictionsWidget(runtime=window.runtime, parent=window.central_widget),
    )
    # Where each one shows up (left tool bar vs. top catalog bar) is decided in ui/registry.py.
    # HTP is an import mode, not a QSAR view — it is wired into the ligand-import flow, not here.


__all__ = ["PredictionsWidget",
           "QSARBuildWidget", "QSARModelsWidget", "register_qsar_panels"]
