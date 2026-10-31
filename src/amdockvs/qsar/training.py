"""Training one model on arrays: what a worker of the train job runs. No SQL here.

The caller has already labeled, featurized and split the molecules; this fits, validates
(holdout, training-only CV, label permutation), ranks the features and returns the registry row
and one member row per molecule, the shapes ``qsar_models`` and ``qsar_model_members`` take.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from amdockvs.qsar.modeling import (
    FittedModel,
    classification_metrics,
    cross_validation_metrics,
    feature_importance,
    fit_model,
    regression_metrics,
    y_scramble_score,
)
from amdockvs.qsar.splitters import assign_split


def feature_type_of(kind: str) -> str:
    return "fingerprints_ecfp4" if kind == "ecfp4" else "descriptors_2d"


def score(task: str, y_true: np.ndarray, fitted: FittedModel, x_matrix: np.ndarray) -> dict[str, Any]:
    """Task-appropriate metrics; for classification, pass positive-class proba for ROC-AUC."""
    if task == "classification":
        y_score = None
        estimator = fitted.estimator
        if hasattr(estimator, "predict_proba") and len(getattr(fitted, "classes", ()) or ()) == 2:
            y_score = np.asarray(estimator.predict_proba(np.asarray(x_matrix, dtype=float)))[:, 1]
        return classification_metrics(y_true, fitted.predict(x_matrix), y_score)
    return regression_metrics(y_true, fitted.predict(x_matrix))


def _optional(values, i: int) -> float | None:
    return None if values is None or not np.isfinite(values[i]) else float(values[i])


def train_on_arrays(
    *, x: np.ndarray, y: np.ndarray, ids, is_test: np.ndarray, folds: np.ndarray, groups, names,
    spec: dict[str, Any],
) -> tuple[FittedModel, dict[str, Any], list[dict[str, Any]]]:
    """(fitted model, registry row, member rows) for one model ``spec``: algorithm, task, kind,
    split, test_size, cv_folds, y_scramble, class_threshold, corr_threshold, hyperparams, seed,
    fp_radius, fp_nbits, name, endpoint."""
    task, kind, algorithm = spec["task"], spec["kind"], spec["algorithm"]
    seed, cv_folds, split = int(spec.get("seed") or 0), int(spec.get("cv_folds") or 0), spec["split"]
    hyperparams, corr = spec.get("hyperparams"), float(spec.get("corr_threshold", 0.95))
    train_idx, test_idx = np.flatnonzero(~is_test), np.flatnonzero(is_test)
    if float(spec.get("test_size") or 0) > 0 and (len(train_idx) < 2 or not len(test_idx)):
        raise ValueError("The holdout leaves fewer than two training molecules or no test molecule "
                         "among those with features. Add molecules or reduce the test fraction.")
    x_train, y_train = x[train_idx], y[train_idx]
    if task == "classification" and np.unique(y_train).size < 2:
        raise ValueError("The split leaves only one class in training. Add more labeled molecules "
                         "or choose a different split or seed.")

    fitted = fit_model(
        x_train, y_train, feature_names=tuple(names), algorithm=algorithm, task=task, hyperparams=hyperparams,
        feature_kind=kind, corr_threshold=corr,
        fp_radius=spec.get("fp_radius") if kind == "ecfp4" else None,
        fp_nbits=spec.get("fp_nbits") if kind == "ecfp4" else None,
    )
    metrics: dict[str, Any] = {
        "identity": spec["identity"],
        "feature_kind": kind,
        "features": f"{kind}:{len(names)}",
        "test_size": float(spec.get("test_size") or 0),
        "seed": seed,
        "class_threshold": spec.get("class_threshold"),
        "classes": [int(c) for c in fitted.classes],
        "positive_class": int(max(fitted.classes)) if len(fitted.classes) == 2 else None,
        "corr_threshold": corr,
        "train": score(task, y_train, fitted, x_train),
    }
    if kind == "ecfp4":
        metrics.update(fp_radius=int(spec["fp_radius"]), fp_nbits=int(spec["fp_nbits"]))
    if len(test_idx):
        metrics["test"] = score(task, y[test_idx], fitted, x[test_idx])
    oof_predicted = oof_probability = None
    train_folds = folds[train_idx]
    if cv_folds >= 2:
        metrics["cv_folds"] = cv_folds
        cv_metrics = cross_validation_metrics(
            x_train, y_train, folds=train_folds, algorithm=algorithm, task=task,
            hyperparams=hyperparams, feature_kind=kind, corr_threshold=corr,
        )
        oof_predicted = cv_metrics.pop("oof_predicted")
        oof_probability = cv_metrics.pop("oof_probability")
        metrics.update(cv_metrics)
        if cv_metrics["q2" if task == "regression" else "cv_mcc"] is None:
            metrics["cv_warning"] = (
                f"CV not computed: {cv_folds} folds need at least as many training groups, "
                "both classes in every fold's training part, and a non-constant response."
            )
    if int(spec.get("y_scramble") or 0) >= 1:
        if cv_folds < 2:  # scrambling is scored out of sample: 3 folds when CV is off
            train_groups = None if groups is None else [groups[i] for i in train_idx]
            train_folds = assign_split(split, y=y_train, task=task, groups=train_groups, cv_folds=3, seed=seed)[1]
        metrics["y_scramble_metric"] = "q2_pooled_oof" if task == "regression" else "cv_mcc"
        metrics["y_scramble"] = y_scramble_score(
            x_train, y_train, folds=train_folds, algorithm=algorithm, task=task,
            n_permutations=int(spec["y_scramble"]), hyperparams=hyperparams, feature_kind=kind,
            corr_threshold=corr, seed=seed,
        )
        if metrics["y_scramble"] is None:
            metrics["y_scramble_warning"] = (
                "Label-permutation validation not computed: too few training molecules or groups "
                "for its folds, or unscorable folds."
            )
    importance = feature_importance(fitted.estimator, tuple(names))
    if importance:
        metrics["feature_importance"] = [[n, v] for n, v in importance]

    predicted = fitted.predict(x)
    probability = fitted.predict_positive_probability(x)
    oof_at = {int(i): k for k, i in enumerate(train_idx)}
    test_scores = metrics.get("test") or {}
    endpoint = str(spec["endpoint"])
    record = {
        "name": str(spec.get("name") or "").strip() or f"{algorithm}_{endpoint}",
        "algorithm": algorithm,
        "target": endpoint,
        "feature_type": feature_type_of(kind),
        "source": "trained",
        "task": task,
        "engine": "sklearn",
        "splitter": split,
        "n_train": len(train_idx),
        "n_test": len(test_idx),
        "test_r2": test_scores.get("r2"),
        "test_rmse": test_scores.get("rmse"),
        "q2": metrics.get("q2"),
        "test_mcc": test_scores.get("mcc"),
        "test_roc_auc": test_scores.get("roc_auc"),
        "cv_mcc": metrics.get("cv_mcc"),
        "metrics": metrics,
    }
    members = [
        {
            "molecule_id": int(mid),
            "role": "test" if is_test[i] else "train",
            "fold": int(folds[i]) if folds[i] >= 0 else None,
            "observed": float(y[i]),
            "predicted": float(predicted[i]),
            "probability": _optional(probability, i),
            "oof_predicted": None if i not in oof_at else _optional(oof_predicted, oof_at[i]),
            "oof_probability": None if i not in oof_at else _optional(oof_probability, oof_at[i]),
        }
        for i, mid in enumerate(ids)
    ]
    return fitted, record, members
