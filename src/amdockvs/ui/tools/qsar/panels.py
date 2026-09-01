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
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from amdockvs.qsar.modeling import supported_algorithms
from amdockvs.ui.async_query import run_async
from ms_components.ms_stepper import Orientation, QStepper

QSAR_MODELS_VIEW_ID = "workspace.qsar_models"
PREDICTIONS_VIEW_ID = "workspace.qsar_predictions"


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
# QSAR models + activities
# ---------------------------------------------------------------------------

class QSARModelsWidget(QWidget):
    def __init__(self, *, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        outer = QVBoxLayout(self)
        # if getattr(runtime, "active_context", None) is None:
        #     label = QLabel("Open or create a project to work with QSAR.", self)
        #     label.setAlignment(Qt.AlignCenter)
        #     outer.addWidget(label)
        #     return

        # Two tabs instead of an accordion: the accordion hid the models list behind the wizard.
        # Each tab scrolls so the tall wizard / tables never deform the central area.
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._scrolled(self._train_box()), "Build model")
        self.tabs.addTab(self._scrolled(self._models_box()), "Models & analysis")
        outer.addWidget(self.tabs, 1)
        outer.addWidget(self.status)
        self.refresh()

    def _scrolled(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(inner)
        return scroll

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
    def _train_box(self) -> QGroupBox:
        """Build-a-model wizard: a QStepper (same component the docking studio uses) over the
        same train controls, laid out as StarDrop's Auto-Modeler steps."""
        box = QGroupBox(self)
        layout = QVBoxLayout(box)

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
        self.stepper.add_step("Descriptors", "features", header_nav=False).add_widget(
            _form_step([("Features", self.feature_source), ("Correlation cutoff", self.corr_threshold),
                        ("", corr_btn), ("", self.morgan_check), ("", step3_desc_btn)]))
        self.stepper.add_step("Methods", "algorithms & run", header_nav=False).add_widget(
            _form_step([("Algorithms", self.algo_box), ("CV folds", self.cv_folds),
                        ("y-scramble runs", self.y_scramble)]))
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
        self.refresh()
        self._populate_comparison(trained)
        msg = f"Trained {len(trained)} model(s); compared below."
        if errors:
            msg += " Failed: " + "; ".join(errors)
        self.status.setText(msg)

    def _on_train_error(self, exc: Exception) -> None:
        self.stepper.setEnabled(True)
        self.status.setText("")
        QMessageBox.warning(self, "QSAR train", str(exc))

    # --- models ---------------------------------------------------------------
    def _models_box(self) -> QGroupBox:
        box = QGroupBox(self)
        layout = QVBoxLayout(box)
        self.models_table = QTableWidget(0, 5, box)
        self.models_table.setHorizontalHeaderLabels(["id", "name", "algorithm", "target", "source"])
        self.models_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.models_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.models_table.horizontalHeader().setStretchLastSection(True)
        self.models_table.setMaximumHeight(170)
        self.models_table.itemSelectionChanged.connect(self._show_model_summary)
        self.models_table.itemChanged.connect(lambda _i: self._update_action_state())
        layout.addWidget(self.models_table)
        self.model_summary = QLabel("Tick the id boxes to pick the model(s) to act on; click a row for its summary.",
                                    box)
        self.model_summary.setWordWrap(True)
        self.model_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.model_summary)
        # This label makes the model→action link explicit: every button below acts on the TICKED models.
        self.acting_label = QLabel("Acting on: no models ticked.", box)
        self.acting_label.setStyleSheet("QLabel { font-weight: bold; }")
        layout.addWidget(self.acting_label)

        self.comparison_table = QTableWidget(0, 6, box)
        self.comparison_table.setHorizontalHeaderLabels(
            ["id", "algorithm", "features", "n_train", "test MCC/R²", "test AUC/RMSE"])
        self.comparison_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.comparison_table.setMaximumHeight(150)
        self.comparison_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.comparison_table)

        # Two labelled groups so it's clear what each button does with the ticked model(s).
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
        self.eval_subset.setToolTip("Which ligands ROC and observed-vs-predicted score on. Test = honest "
                                    "predictivity; Train vs Test gap = overfitting; All = everything labeled.")

        use_row = QHBoxLayout()
        use_row.addWidget(QLabel("Use the model:", box))
        use_row.addWidget(
            _btn("compare", "Compare", "Fill the comparison table from the ticked models.", self._compare_checked))
        use_row.addWidget(
            _btn("predict", "Predict all ligands", "Score every ligand with each ticked model.", self._predict))
        use_row.addWidget(
            _btn("evaluate", "Evaluate", "Metrics of each ticked model on its labeled ligands.", self._evaluate))
        use_row.addWidget(
            _btn("plot", "Observed vs predicted", "Model-fit scatter; several ticked models overlay as series.",
                 self._plot_fit))
        use_row.addWidget(QLabel("on:", box))
        use_row.addWidget(self.eval_subset)
        use_row.addStretch(1)
        layout.addLayout(use_row)

        analyze_row = QHBoxLayout()
        analyze_row.addWidget(QLabel("Analyze:", box))
        analyze_row.addWidget(_btn("importance", "Feature importance",
                                   "Bar chart of the ticked tree model's features (tick exactly one).",
                                   self._show_importance))
        analyze_row.addWidget(
            _btn("roc", "ROC curve", "ROC + AUC; several ticked models overlay (classification).", self._show_roc))
        analyze_row.addWidget(
            _btn("ad", "Applicability domain", "Tanimoto to the training set (tick one; can be slow for big sets).",
                 self._show_ad))
        analyze_row.addWidget(
            _btn("glowing", "Glowing molecule…", "Per-atom contribution — ECFP4 models only (tick one).",
                 self._glowing_molecule))
        analyze_row.addStretch(1)
        layout.addLayout(analyze_row)
        self._update_action_state()
        return box

    # --- action targeting (everything acts on the TICKED models) --------------
    def _checked_rows(self) -> list[int]:
        table = self.models_table
        return [r for r in range(table.rowCount())
                if table.item(r, 0) is not None and table.item(r, 0).checkState() == Qt.Checked]

    def _checked_models(self) -> list:
        models = getattr(self, "_models", [])
        return [models[r] for r in self._checked_rows() if r < len(models)]

    def _update_action_state(self) -> None:
        """Enable/disable each button by how many models are ticked + their feature kind, and
        spell out the current target — so the buttons visibly relate to the model selection."""
        if not getattr(self, "_action_buttons", None):
            return
        checked = self._checked_models()
        n = len(checked)
        kinds = {str((m.metrics or {}).get("feature_kind", "")) for m in checked}
        tasks = {str((m.metrics or {}).get("task", "")) for m in checked}
        one_ecfp4 = n == 1 and kinds == {"ecfp4"}
        all_regression = n >= 1 and tasks == {"regression"}
        all_classification = n >= 1 and tasks == {"classification"}
        rules = {
            "compare": n >= 1, "predict": n >= 1, "evaluate": n >= 1,
            "plot": all_regression,  # observed-vs-predicted is a regression diagnostic
            "roc": all_classification,  # ROC is classification-only
            "importance": n == 1, "ad": n == 1, "glowing": one_ecfp4,
        }
        for key, enabled in rules.items():
            self._action_buttons[key].setEnabled(bool(enabled))
        self._action_buttons["plot"].setToolTip(
            "Observed vs predicted (test set) — regression models only." if not all_regression
            else "Observed vs predicted on the held-out test ligands; several ticked models overlay.")
        self._action_buttons["roc"].setToolTip(
            "ROC + AUC — classification models only." if not all_classification
            else "ROC + AUC over the labeled ligands; several ticked models overlay.")
        self._action_buttons["glowing"].setToolTip(
            "Per-atom contribution — ECFP4 models only (tick exactly one ECFP4 model)."
            if not one_ecfp4 else "Per-atom contribution of a ligand to this ECFP4 model.")
        if n == 0:
            self.acting_label.setText("Acting on: no models ticked — tick id boxes above.")
        else:
            names = ", ".join(f"#{m.id} {m.name}" for m in checked[:4]) + (" …" if n > 4 else "")
            self.acting_label.setText(f"Acting on {n} model(s): {names}")

    def _require_checked(self, *, exactly_one: bool = False) -> list:
        checked = self._checked_models()
        if not checked:
            QMessageBox.information(self, "QSAR", "Tick at least one model (id box) to act on.")
            return []
        if exactly_one and len(checked) != 1:
            QMessageBox.information(self, "QSAR", "Tick exactly one model for this action.")
            return []
        return checked

    def _plot_fit(self) -> None:
        checked = self._require_checked()
        if not checked:
            return
        if any(str((m.metrics or {}).get("task", "")) != "regression" for m in checked):
            QMessageBox.information(self, "QSAR", "Observed-vs-predicted is a regression diagnostic. For "
                                                  "classification use ROC curve / Evaluate instead.")
            return
        show = getattr(self.window(), "show_model_fit_series", None)
        if show is None:
            return
        model_specs = [(int(m.id), m.name, str(m.target or "")) for m in checked]
        subset = self.eval_subset.currentData()

        def _compute():
            series = []
            for mid, name, endpoint in model_specs:
                self.runtime.qsar.predict(model=mid)
                preds = {p.molecule_id: p.value for p in self.runtime.qsar.list_predictions(model=mid)}
                acts = {r["molecule_id"]: r["value"] for r in
                        self.runtime.qsar.activity_rows(endpoint=endpoint or None)}
                # Restrict to the chosen subset (test = honest); 'all' or a missing split keeps everything.
                assign = self.runtime.qsar.model_subsets(model=mid)
                keep = (lambda k: assign.get(k) == subset) if subset in ("train", "test") and assign else (
                    lambda k: True)
                points = [(acts[k], preds[k]) for k in preds if k in acts and keep(k)]
                series.append({"label": f"#{mid} {name}", "points": points})
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
        checked = self._require_checked(exactly_one=True)
        if not checked:
            return
        metrics = dict(checked[0].metrics or {})
        pairs = metrics.get("feature_importance") or []
        if not pairs:
            QMessageBox.information(self, "QSAR", "This model exposes no feature importances "
                                                  "(only tree models do; linear/kNN/SVC don't).")
            return
        show = getattr(self.window(), "show_feature_importance", None)
        if show is not None:
            show(f"#{checked[0].id} {metrics.get('feature_kind', '')}", [(str(n), float(v)) for n, v in pairs])
            self.status.setText(f"Top {min(len(pairs), 15)} features of #{checked[0].id}.")

    def _show_roc(self) -> None:
        checked = self._require_checked()
        if not checked:
            return
        show = getattr(self.window(), "show_roc_curves", None)
        if show is None:
            return
        specs = [(int(m.id), m.name) for m in checked]
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
            if curves:
                show(curves)
            msg = f"ROC [{subset}]: " + ", ".join(c["label"] for c in curves) if curves else "No ROC curves produced."
            if failed:
                msg += " | Skipped: " + "; ".join(failed)
            self.status.setText(msg)

        run_async(_compute, _done, on_error=lambda exc: QMessageBox.warning(self, "QSAR ROC", str(exc)), busy=self)

    def _show_ad(self) -> None:
        checked = self._require_checked(exactly_one=True)
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

    def _glowing_molecule(self) -> None:
        from amdockvs.ui.tools.qsar.activities import pick_ligands

        checked = self._require_checked(exactly_one=True)
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
        checked = self._require_checked()
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

    def _evaluate(self) -> None:
        checked = self._require_checked()
        if not checked:
            return
        specs = [(int(m.id), m.name) for m in checked]

        def _work():
            return [(mid, name, self.runtime.qsar.evaluate(model=mid)) for mid, name in specs]

        def _fmt(res: dict) -> str:
            keys = ("mcc", "roc_auc", "r2", "rmse")
            return ", ".join(f"{k}={res[k]:.3f}" for k in keys if isinstance(res.get(k), float))

        run_async(
            _work,
            lambda out: self.status.setText("Evaluate — " + "; ".join(
                f"#{mid} {name}: {_fmt(res)}" for mid, name, res in out)),
            on_error=lambda exc: QMessageBox.warning(self, "QSAR evaluate", str(exc)),
            busy=self,
        )

    def refresh(self) -> None:
        run_async(self.runtime.qsar.list_models, self._fill_models, on_error=lambda _e: None, busy=self.models_table)
        run_async(self.runtime.qsar.endpoint_kinds, self._fill_endpoints, on_error=lambda _e: None,
                  busy=self.models_table)

    refresh_view = refresh

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

    def _fill_models(self, models) -> None:
        self._models = list(models)
        self.models_table.blockSignals(True)  # setCheckState below would spam itemChanged → _update_action_state
        self.models_table.setRowCount(len(models))
        for r, m in enumerate(models):
            for c, val in enumerate([m.id, m.name, m.algorithm, m.target, m.source]):
                item = QTableWidgetItem(str(val))
                if c == 0:  # id column doubles as the ticked-set checkbox (drives every action)
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Unchecked)
                self.models_table.setItem(r, c, item)
        self.models_table.blockSignals(False)
        self._update_action_state()

    @staticmethod
    def _comparison_cells(metrics: dict) -> list[str]:
        """[features, n_train, primary, secondary]. Classification → MCC + ROC-AUC (honest under
        imbalance); regression → R² + RMSE."""
        test = metrics.get("test") or {}
        if metrics.get("task") == "classification":
            primary, secondary = test.get("mcc"), test.get("roc_auc")
        else:
            primary, secondary = test.get("r2"), test.get("rmse")
        return [
            str(metrics.get("feature_kind", "?")),
            str(metrics.get("n_train", "?")),
            "—" if primary is None else f"{primary:.3f}",
            "—" if secondary is None else f"{secondary:.3f}",
        ]

    def _populate_comparison(self, rows: list[dict]) -> None:
        """rows = [{id, algorithm, metrics}, …] (trained batch or ticked models)."""
        self.comparison_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = [str(row["id"]), str(row.get("algorithm", ""))] + self._comparison_cells(row.get("metrics") or {})
            for c, val in enumerate(cells):
                self.comparison_table.setItem(r, c, QTableWidgetItem(val))

    def _compare_checked(self) -> None:
        models = getattr(self, "_models", [])
        ticked = [
            {"id": m.id, "algorithm": m.algorithm, "metrics": m.metrics}
            for r, m in enumerate(models)
            if self.models_table.item(r, 0) is not None and self.models_table.item(r, 0).checkState() == Qt.Checked
        ]
        if not ticked:
            QMessageBox.information(self, "QSAR", "Tick the id boxes of the models to compare.")
            return
        self._populate_comparison(ticked)

    def _show_model_summary(self) -> None:
        row = self.models_table.currentRow()
        models = getattr(self, "_models", [])
        if row < 0 or row >= len(models):
            self.model_summary.setText("Select a model to see its summary.")
            return
        m = models[row].metrics or {}

        def fmt(d):
            return ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                             for k, v in (d or {}).items() if k != "n_samples")

        parts = [f"<b>{models[row].name}</b> — {models[row].algorithm}, features={m.get('feature_kind', '?')}, "
                 f"split={m.get('split', '?')}, n_train={m.get('n_train', '?')}"]
        if m.get("train"):
            parts.append(f"train: {fmt(m['train'])}")
        if m.get("test"):
            parts.append(f"test: {fmt(m['test'])}")
        for key in ("q2", "cv_mcc", "cv_accuracy", "y_scramble"):
            if m.get(key) is not None:
                parts.append(f"{key}={m[key]:.3f}")
        self.model_summary.setText("<br>".join(parts))


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
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Ligand id", "Name", "Predicted", "Confidence"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_select_row)
        outer.addWidget(self.table, 1)
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.refresh()

    def _current_model_id(self) -> int | None:
        return self.model_combo.currentData()

    def refresh(self) -> None:
        run_async(self.runtime.qsar.list_models, self._fill_models, on_error=lambda _e: None, busy=self.table)

    refresh_view = refresh

    def _fill_models(self, models) -> None:
        current = self._current_model_id()
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
            for c, val in enumerate([row["molecule_id"], row["name"], f"{row['value']:.4f}", conf]):
                self.table.setItem(r, c, QTableWidgetItem(str(val)))
        self.status.setText(f"{len(rows)} prediction(s). Select a row to glow the molecule."
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
        if row < 0 or model_id is None or self.table.item(row, 0) is None:
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


def register_qsar_panels(window) -> None:
    window.register_main_view(
        QSAR_MODELS_VIEW_ID, "QSAR Models",
        lambda: QSARModelsWidget(runtime=window.runtime, parent=window.central_widget),
    )
    window.register_main_view(
        PREDICTIONS_VIEW_ID, "Predictions",
        lambda: PredictionsWidget(runtime=window.runtime, parent=window.central_widget),
    )
    # Nav entry is added in main_window._populate_navigation_docks (qsar_dock list).
    # HTP is an import mode, not a QSAR view — it is wired into the ligand-import flow, not here.


__all__ = ["PREDICTIONS_VIEW_ID", "QSAR_MODELS_VIEW_ID", "PredictionsWidget", "QSARModelsWidget",
           "register_qsar_panels"]
