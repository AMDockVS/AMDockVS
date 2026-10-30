"""Simple QSAR model templates over scikit-learn.

Deliberately a thin catalogue of ready-to-use estimators — not a pycaret/chemprop.
Each algorithm is a one-line builder; scale-sensitive ones get a StandardScaler in a
Pipeline, tree ensembles don't. Models are pickled whole (estimator + metadata) with
joblib so predict() needs no schema re-derivation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin


class CorrelationFilter(BaseEstimator, TransformerMixin):
    """Drop one of every pair of features with |Pearson r| > threshold (keeps the first seen).
    Greedy and O(n²) in features — fine for ≤~300 descriptors, not for fingerprints (skip it there).
    Zero-variance columns give NaN correlations, treated as 0 so they aren't dropped here (a prior
    VarianceThreshold removes them)."""

    def __init__(self, threshold: float = 0.95):
        self.threshold = threshold

    def fit(self, X, y=None):
        X = np.asarray(X, dtype=float)
        n = X.shape[1]
        keep = np.ones(n, dtype=bool)
        if n > 1:
            with np.errstate(invalid="ignore", divide="ignore"):
                corr = np.nan_to_num(np.corrcoef(X, rowvar=False), nan=0.0)
            for j in range(n):
                if not keep[j]:
                    continue
                for k in range(j + 1, n):
                    if keep[k] and abs(corr[j, k]) > float(self.threshold):
                        keep[k] = False
        self.support_ = keep
        return self

    def transform(self, X):
        return np.asarray(X, dtype=float)[:, self.support_]

    def get_support(self):
        return self.support_

# Numeric descriptor columns available on MoleculeRecord (populated by the descriptor job).
DEFAULT_QSAR_FEATURES: tuple[str, ...] = (
    "mw",
    "logp",
    "hbd",
    "hba",
    "tpsa",
    "rotatable_bonds",
    "ring_count",
    "aromatic_ring_count",
    "hetero_atom_count",
    "heavy_atom_count",
    "fraction_csp3",
    "formal_charge",
    "fragment_count",
)
ALLOWED_FEATURES = frozenset(DEFAULT_QSAR_FEATURES)

# Algorithms that benefit from feature scaling -> get a StandardScaler in the pipeline.
_SCALE_SENSITIVE = frozenset(
    {"linear_regression", "ridge", "lasso", "svr", "knn", "logistic_regression", "svc"}
)
# Classifiers that accept class_weight='balanced' — the honest default for imbalanced endpoints
# (e.g. Tox21: 96% inactive). GradientBoosting/kNN/baseline don't take it, so they're excluded.
_BALANCEABLE = frozenset({"logistic_regression", "random_forest", "svc"})


def _build_estimator(
    algorithm: str,
    task: str,
    hyperparams: dict[str, Any],
    *,
    feature_kind: str = "descriptors",
    corr_threshold: float = 0.95,
):
    """The estimator as a leakage-safe Pipeline. For descriptor features the preprocessing
    (impute → drop zero-variance → drop correlated → scale) is *inside* the pipeline, so it
    refits per CV fold and is serialized whole with the model. Fingerprint (ecfp4) features skip
    preprocessing — binary bits don't scale and an O(n²) correlation filter over 2048 bits is silly."""
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.feature_selection import VarianceThreshold
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Lasso, LinearRegression, LogisticRegression, Ridge
    from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC, SVR

    hp = dict(hyperparams or {})
    balance = {"class_weight": "balanced"} if algorithm in _BALANCEABLE else {}
    regressors = {
        "baseline": lambda: DummyRegressor(strategy="mean"),
        "linear_regression": lambda: LinearRegression(**hp),
        "ridge": lambda: Ridge(**{"alpha": 1.0, **hp}),
        "lasso": lambda: Lasso(**{"alpha": 0.1, **hp}),
        "random_forest": lambda: RandomForestRegressor(**{"n_estimators": 300, "random_state": 0, "n_jobs": -1, **hp}),
        "gradient_boosting": lambda: GradientBoostingRegressor(**{"random_state": 0, **hp}),
        "svr": lambda: SVR(**hp),
        "knn": lambda: KNeighborsRegressor(**hp),
    }
    classifiers = {
        "baseline": lambda: DummyClassifier(strategy="prior"),
        "logistic_regression": lambda: LogisticRegression(**{"max_iter": 1000, **balance, **hp}),
        "random_forest": lambda: RandomForestClassifier(**{"n_estimators": 300, "random_state": 0, "n_jobs": -1, **balance, **hp}),
        "gradient_boosting": lambda: GradientBoostingClassifier(**{"random_state": 0, **hp}),
        "svc": lambda: SVC(**{"probability": True, **balance, **hp}),
        "knn": lambda: KNeighborsClassifier(**hp),
    }
    table = regressors if task == "regression" else classifiers
    if algorithm not in table:
        raise ValueError(
            f"Unsupported {task} algorithm '{algorithm}'. Supported: {sorted(table)}"
        )
    model = table[algorithm]()
    if feature_kind == "ecfp4":
        return model
    steps: list[tuple[str, Any]] = [
        ("impute", SimpleImputer(strategy="median")),
        ("variance", VarianceThreshold(0.0)),
        ("correlation", CorrelationFilter(threshold=float(corr_threshold))),
    ]
    if algorithm in _SCALE_SENSITIVE:
        steps.append(("scaler", StandardScaler()))
    steps.append(("model", model))
    return Pipeline(steps)


def supported_algorithms(task: str = "regression") -> tuple[str, ...]:
    if task == "classification":
        return ("baseline", "logistic_regression", "random_forest", "gradient_boosting", "svc", "knn")
    return ("baseline", "linear_regression", "ridge", "lasso", "random_forest", "gradient_boosting", "svr", "knn")


SUPPORTED_QSAR_ALGORITHMS = frozenset(supported_algorithms("regression") + supported_algorithms("classification"))


def normalize_feature_names(feature_names: Sequence[str] | None) -> tuple[str, ...]:
    names = tuple(str(n).strip() for n in (feature_names or DEFAULT_QSAR_FEATURES) if str(n).strip())
    if not names:
        raise ValueError("QSAR requires at least one feature.")
    invalid = sorted(n for n in names if n not in ALLOWED_FEATURES)
    if invalid:
        raise ValueError(f"Unsupported QSAR feature names: {invalid}. Allowed: {sorted(ALLOWED_FEATURES)}")
    return names


@dataclass
class FittedModel:
    estimator: Any
    task: str
    algorithm: str
    feature_names: tuple[str, ...]
    classes: tuple[Any, ...] = field(default_factory=tuple)
    residual_std: float | None = None
    # "descriptors" (named MoleculeRecord columns) or "ecfp4" (Morgan bit vector). For ecfp4 the
    # api rebuilds X from FingerprintRecord at predict time using these.
    feature_kind: str = "descriptors"
    fp_radius: int | None = None
    fp_nbits: int | None = None

    def predict(self, x_matrix: np.ndarray) -> np.ndarray:
        return np.asarray(self.estimator.predict(np.asarray(x_matrix, dtype=float)))

    def predict_confidence(self, x_matrix: np.ndarray) -> np.ndarray | None:
        """Per-sample confidence: max class probability (classification) or the model's
        residual std broadcast (regression). None when unavailable."""
        x = np.asarray(x_matrix, dtype=float)
        if self.task == "classification" and hasattr(self.estimator, "predict_proba"):
            return np.max(self.estimator.predict_proba(x), axis=1)
        if self.residual_std is not None:
            return np.full(x.shape[0], float(self.residual_std))
        return None

    def predict_positive_probability(self, x_matrix: np.ndarray) -> np.ndarray | None:
        """P(the higher-valued class) for binary classifiers; None for other models.

        Keep this separate from max-class confidence: a confident negative prediction has a
        low positive probability. Read class order from the estimator that produced the columns.
        """
        if self.task != "classification" or not hasattr(self.estimator, "predict_proba"):
            return None
        classes = np.asarray(getattr(self.estimator, "classes_", self.classes))
        if classes.size != 2:
            return None
        probabilities = np.asarray(self.estimator.predict_proba(np.asarray(x_matrix, dtype=float)))
        return probabilities[:, int(np.argmax(classes))]


def fit_model(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    feature_names: Sequence[str],
    algorithm: str,
    task: str = "regression",
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "descriptors",
    corr_threshold: float = 0.95,
    fp_radius: int | None = None,
    fp_nbits: int | None = None,
) -> FittedModel:
    if task not in {"regression", "classification"}:
        raise ValueError(f"task must be 'regression' or 'classification', got {task!r}.")
    x_matrix = np.asarray(x_matrix, dtype=float)
    if x_matrix.ndim != 2:
        raise ValueError("fit_model requires a 2D feature matrix.")
    if x_matrix.shape[0] < 2:
        raise ValueError("QSAR training requires at least two samples.")
    y_vector = np.asarray(y_vector)
    estimator = _build_estimator(
        algorithm, task, hyperparams or {}, feature_kind=feature_kind, corr_threshold=corr_threshold
    )
    y_fit = y_vector.astype(float) if task == "regression" else y_vector.astype(int)
    estimator.fit(x_matrix, y_fit)
    residual_std = None
    classes: tuple[Any, ...] = ()
    if task == "regression":
        residuals = y_fit - np.asarray(estimator.predict(x_matrix), dtype=float)
        residual_std = float(np.std(residuals, ddof=1)) if y_fit.shape[0] > 1 else None
    else:
        classes = tuple(getattr(estimator, "classes_", ()))
    return FittedModel(
        estimator=estimator,
        task=task,
        algorithm=algorithm,
        feature_names=tuple(str(n) for n in feature_names),
        classes=classes,
        residual_std=residual_std,
        feature_kind=feature_kind,
        fp_radius=fp_radius,
        fp_nbits=fp_nbits,
    )


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    n = int(y_true.shape[0])
    if n == 0:
        raise ValueError("regression_metrics requires at least one sample.")
    error = y_true - y_pred
    ss_res = float(np.sum(error ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    return {
        "n_samples": n,
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "r2": None if n < 2 or ss_tot == 0.0 else float(1.0 - ss_res / ss_tot),
    }


def classification_metrics(
    y_true: np.ndarray, y_pred: np.ndarray, y_score: np.ndarray | None = None
) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score,
        average_precision_score,
        balanced_accuracy_score,
        f1_score,
        matthews_corrcoef,
        roc_auc_score,
    )

    y_true = np.asarray(y_true).astype(int)
    y_pred = np.asarray(y_pred).astype(int)
    n = int(y_true.shape[0])
    if n == 0:
        raise ValueError("classification_metrics requires at least one sample.")
    both_classes = len(set(y_true.tolist())) == 2
    metrics: dict[str, Any] = {
        "n_samples": n,
        "accuracy": float(accuracy_score(y_true, y_pred)),
        # MCC and balanced accuracy are the honest metrics under imbalance — accuracy inflates
        # when one class dominates (predict-all-majority already scores ~majority fraction).
        "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
        "f1": float(f1_score(y_true, y_pred, average="weighted", zero_division=0)),
        "mcc": float(matthews_corrcoef(y_true, y_pred)) if both_classes else None,
    }
    # Score-based metrics need the positive-class probability and both classes present; binary only.
    if y_score is not None and both_classes:
        score = np.asarray(y_score, dtype=float)
        try:
            metrics["roc_auc"] = float(roc_auc_score(y_true, score))
        except ValueError:
            metrics["roc_auc"] = None
        try:
            metrics["pr_auc"] = float(average_precision_score(y_true, score))  # better than ROC under imbalance
        except ValueError:
            metrics["pr_auc"] = None
        metrics["enrichment_1pct"] = _enrichment_factor(y_true, score, fraction=0.01)  # virtual-screening yield
    return metrics


def _enrichment_factor(y_true: np.ndarray, y_score: np.ndarray, *, fraction: float = 0.01) -> float | None:
    """Enrichment at the top `fraction` of scores: (actives found in top-k / k) / (overall active rate).
    EF=1 is random, higher is better — the number a virtual-screening chemist actually cares about."""
    n = int(y_true.shape[0])
    k = max(1, int(round(float(fraction) * n)))
    active_rate = float(np.mean(y_true))
    if n == 0 or active_rate == 0.0:
        return None
    top = np.argsort(-np.asarray(y_score, dtype=float))[:k]
    return float(np.mean(np.asarray(y_true)[top]) / active_rate)


def grouped_holdout_split(groups: Sequence[Any], *, test_size: float, seed: int = 0) -> tuple[list[int], list[int]]:
    """Scaffold-style split: return (train_idx, test_idx) sending whole groups to the holdout until
    ~test_size of the rows are held out. Guarantees no group spans both sets. # ponytail: greedy
    fill in shuffled-group order, no class balancing."""
    if not 0 < float(test_size) < 1:
        raise ValueError("Scaffold holdout fraction must be between 0 and 1.")
    n = len(groups)
    import random

    by_group: dict[Any, list[int]] = {}
    for idx, key in enumerate(groups):
        by_group.setdefault(key, []).append(idx)
    if len(by_group) < 2:
        raise ValueError(
            "Scaffold holdout requires at least two distinct scaffold groups; "
            f"found {len(by_group)}. Add more scaffold diversity, choose a random split, "
            "or explicitly set the test fraction to 0 (no holdout)."
        )
    order = list(by_group)
    random.Random(int(seed)).shuffle(order)
    target = max(1, int(round(float(test_size) * n)))
    test_idx: list[int] = []
    for key in order[:-1]:  # reserve one whole group for training, even for a large test fraction
        if len(test_idx) >= target:
            break
        test_idx.extend(by_group[key])
    test_set = set(test_idx)
    train_idx = [i for i in range(n) if i not in test_set]
    return train_idx, test_idx


def cross_validation_metrics(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    algorithm: str,
    task: str,
    cv: int,
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "descriptors",
    corr_threshold: float = 0.95,
    groups: Sequence[Any] | None = None,
) -> dict[str, float | None]:
    """Score one out-of-fold prediction per training molecule, refitting the full pipeline.

    Regression Q² = 1 - PRESS / sum((y - mean(y))²), using the complete training response
    vector and its out-of-fold predictions. Mean fold R² is a separate, unweighted diagnostic;
    it is undefined if any fold has fewer than two responses or constant responses. Q² is
    undefined for a constant training response. Negative scores are retained.

    Classification retains mean fold MCC. Grouped CV never falls back to row-wise folds.
    Unusable splits return None metrics; invalid inputs or estimator failures still raise.
    """
    from sklearn.base import clone
    from sklearn.metrics import matthews_corrcoef
    from sklearn.model_selection import GroupKFold, check_cv

    x_matrix = np.asarray(x_matrix, dtype=float)
    y_fit = np.asarray(y_vector, dtype=float if task == "regression" else int)
    if task not in {"regression", "classification"}:
        raise ValueError("CV task must be 'regression' or 'classification'.")
    if x_matrix.ndim != 2 or y_fit.ndim != 1 or len(y_fit) != len(x_matrix):
        raise ValueError("CV requires a 2D feature matrix and one response per molecule.")
    result = ({"q2": None, "cv_r2_mean": None, "cv_rmse": None, "cv_press": None}
              if task == "regression" else {"cv_mcc": None})
    folds = int(cv)
    if groups is not None:
        groups = np.asarray(groups)
        if groups.ndim != 1 or len(groups) != x_matrix.shape[0]:
            raise ValueError("CV groups must have one entry per training molecule.")
    if folds < 2 or len(x_matrix) < folds or (groups is not None and len(np.unique(groups)) < folds):
        return result
    splitter = check_cv(GroupKFold(n_splits=folds) if groups is not None else folds,
                        y=y_fit, classifier=task == "classification")
    estimator = _build_estimator(
        algorithm, task, hyperparams or {}, feature_kind=feature_kind, corr_threshold=corr_threshold
    )
    predictions = np.empty(len(y_fit), dtype=float)
    scores = []
    for train, test in splitter.split(x_matrix, y_fit, groups):
        fitted = clone(estimator).fit(x_matrix[train], y_fit[train])
        predicted = np.asarray(fitted.predict(x_matrix[test]))
        predictions[test] = predicted
        scores.append(regression_metrics(y_fit[test], predicted)["r2"] if task == "regression"
                      else float(matthews_corrcoef(y_fit[test], predicted)))
    if not np.all(np.isfinite(predictions)):
        return result
    mean_score = (float(np.mean(scores))
                  if all(score is not None and np.isfinite(score) for score in scores) else None)
    if task == "classification":
        return {"cv_mcc": mean_score}
    press = float(np.sum((y_fit - predictions) ** 2))
    tss = float(np.sum((y_fit - np.mean(y_fit)) ** 2))
    return {"q2": float(1 - press / tss) if tss > 0 else None,
            "cv_r2_mean": mean_score, "cv_rmse": float(np.sqrt(press / len(y_fit))), "cv_press": press}


def cross_val_score_mean(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    algorithm: str,
    task: str,
    cv: int,
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "descriptors",
    corr_threshold: float = 0.95,
    groups: Sequence[Any] | None = None,
) -> float | None:
    """Mean fold R² (regression) or MCC (classification), retained for existing callers.

    This is NOT pooled Q². Use cross_validation_metrics to obtain that statistic.
    """
    metrics = cross_validation_metrics(x_matrix, y_vector, algorithm=algorithm, task=task, cv=cv,
        hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=corr_threshold, groups=groups)
    return metrics["cv_r2_mean" if task == "regression" else "cv_mcc"]


def y_scramble_score(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    algorithm: str,
    task: str,
    n_permutations: int = 10,
    cv: int = 3,
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "descriptors",
    corr_threshold: float = 0.95,
    seed: int = 0,
    groups: Sequence[Any] | None = None,
) -> float | None:
    """OECD y-randomisation: shuffle labels `n_permutations` times and score each *out of sample*
    (k-fold CV), returning mean pooled Q² (regression) or mean CV MCC (classification).
    This uses the same metric definition as the unpermuted validation. Must use CV, not a train-fit score — a
    high-capacity model (RF) memorises shuffled labels in-sample and would always look bad. If the
    real model's CV score isn't clearly above this, its signal is chance. # ponytail: opt-in and
    O(n_permutations·cv·fit) — keep n_permutations small (~5–10)."""
    x_matrix = np.asarray(x_matrix, dtype=float)
    n = int(n_permutations)
    if n < 1 or x_matrix.shape[0] < max(2, int(cv)):
        return None
    rng = np.random.default_rng(int(seed))
    y_fit = y_vector.astype(float) if task == "regression" else y_vector.astype(int)
    scores: list[float] = []
    for _ in range(n):
        shuffled = rng.permutation(y_fit)
        if task == "classification" and np.unique(shuffled).size < 2:
            continue
        metrics = cross_validation_metrics(
            x_matrix, shuffled, algorithm=algorithm, task=task, cv=int(cv),
            hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=corr_threshold,
            groups=groups,
        )
        score = metrics["q2" if task == "regression" else "cv_mcc"]
        if score is not None:
            scores.append(float(score))
    return float(np.mean(scores)) if scores else None


def _surviving_feature_names(estimator, feature_names: Sequence[str]) -> list[str]:
    """Feature names left after the pipeline's variance+correlation selectors dropped columns,
    in order. Non-pipeline estimators (ecfp4) pass names through unchanged."""
    names = [str(n) for n in feature_names]
    steps = getattr(estimator, "named_steps", {}) or {}
    for key in ("variance", "correlation"):  # applied in this order in _build_estimator
        step = steps.get(key)
        if step is not None and hasattr(step, "get_support"):
            mask = list(step.get_support())
            if len(mask) == len(names):
                names = [n for n, keep in zip(names, mask) if keep]
    return names


def feature_importance(estimator, feature_names: Sequence[str], *, top_n: int = 15) -> list[tuple[str, float]]:
    """Top-N (name, weight) by magnitude: tree importances, or the signed coefficients of a
    linear model (comparable because the pipeline scales its features); [] when the estimator
    exposes neither. Names are mapped through the selectors so they line up with the
    estimator's retained features."""
    model = estimator.named_steps["model"] if hasattr(estimator, "named_steps") else estimator
    importances = getattr(model, "feature_importances_", None)
    if importances is None:
        coef = getattr(model, "coef_", None)
        if coef is None:
            return []
        coef = np.atleast_2d(np.asarray(coef, dtype=float))
        # One row is a regression or a binary classifier: keep the sign. Several rows are one
        # per class, where a sign has no single meaning: report the mean magnitude.
        importances = coef[0] if coef.shape[0] == 1 else np.abs(coef).mean(axis=0)
    names = _surviving_feature_names(estimator, feature_names)
    if len(names) != len(importances):  # selection changed shape unexpectedly — fall back to indices
        names = [f"f{i}" for i in range(len(importances))]
    pairs = sorted(zip(names, (float(v) for v in importances)), key=lambda kv: abs(kv[1]), reverse=True)
    return pairs[: int(top_n)]


def save_model(path: str | Path, model: FittedModel) -> Path:
    import joblib

    artifact = Path(path).expanduser().resolve()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact)
    return artifact


def load_model(path: str | Path) -> FittedModel:
    import joblib

    model = joblib.load(Path(path).expanduser().resolve())
    if not isinstance(model, FittedModel):
        raise ValueError(f"Artifact is not a FittedModel: {path}")
    return model


__all__ = [
    "ALLOWED_FEATURES",
    "DEFAULT_QSAR_FEATURES",
    "CorrelationFilter",
    "FittedModel",
    "SUPPORTED_QSAR_ALGORITHMS",
    "classification_metrics",
    "cross_val_score_mean",
    "cross_validation_metrics",
    "feature_importance",
    "fit_model",
    "grouped_holdout_split",
    "load_model",
    "normalize_feature_names",
    "regression_metrics",
    "save_model",
    "supported_algorithms",
    "y_scramble_score",
]
