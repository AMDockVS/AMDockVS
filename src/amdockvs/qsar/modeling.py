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

SKLEARN_MISSING = (
    "QSAR models need scikit-learn, which is not installed. Install it from Settings > External "
    "tools (scikit-learn), or run: pip install 'amdockvs[qsar]'."
)


def require_sklearn() -> None:
    """Raise the install hint when scikit-learn is missing. It is an optional dependency, installed
    on demand into this environment: no restart, every sklearn import here is lazy."""
    try:
        import sklearn  # noqa: F401
    except ImportError:
        raise RuntimeError(SKLEARN_MISSING) from None


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
    feature_kind: str = "rdkit2d",
    corr_threshold: float = 0.95,
):
    """The estimator as a leakage-safe Pipeline. For descriptor features the preprocessing
    (impute → drop zero-variance → drop correlated → scale) is *inside* the pipeline, so it
    refits per CV fold and is serialized whole with the model. Fingerprint (ecfp4) features skip
    preprocessing — binary bits don't scale and an O(n²) correlation filter over 2048 bits is silly."""
    require_sklearn()
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

    from amdockvs.qsar.correlation import CorrelationFilter

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


@dataclass
class FittedModel:
    estimator: Any
    task: str
    algorithm: str
    feature_names: tuple[str, ...]
    classes: tuple[Any, ...] = field(default_factory=tuple)
    residual_std: float | None = None
    # "rdkit2d" (the RDKit 2D block, by feature_names) or "ecfp4" (Morgan bit vector, rebuilt
    # from FingerprintRecord at predict time with fp_radius/fp_nbits).
    feature_kind: str = "rdkit2d"
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
    feature_kind: str = "rdkit2d",
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


def cross_validation_metrics(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    folds: np.ndarray,
    algorithm: str,
    task: str,
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "rdkit2d",
    corr_threshold: float = 0.95,
) -> dict[str, Any]:
    """Refit the full pipeline once per fold (``folds[i]`` = the fold of row i, from
    ``splitters.assign_split``) and predict each molecule with the model that did not see it.

    Regression Q² = 1 - PRESS / sum((y - mean(y))²) over the pooled out-of-fold predictions. Mean
    fold R² is a separate, unweighted diagnostic; undefined if any fold has fewer than two or
    constant responses. Q² is undefined for a constant response. Negative scores are kept.
    Classification reports mean fold MCC.

    Also returns ``oof_predicted`` and, for binary classifiers, ``oof_probability`` (P of the
    higher-valued class): one value per row, or None when CV could not run (fewer than two folds,
    a fold whose training part holds a single class).
    """
    require_sklearn()
    from sklearn.base import clone
    from sklearn.metrics import matthews_corrcoef

    x_matrix = np.asarray(x_matrix, dtype=float)
    y_fit = np.asarray(y_vector, dtype=float if task == "regression" else int)
    folds = np.asarray(folds, dtype=int)
    if task not in {"regression", "classification"}:
        raise ValueError("CV task must be 'regression' or 'classification'.")
    if x_matrix.ndim != 2 or y_fit.ndim != 1 or not len(y_fit) == len(x_matrix) == len(folds):
        raise ValueError("CV requires a 2D feature matrix, one response and one fold per molecule.")
    result: dict[str, Any] = ({"q2": None, "cv_r2_mean": None, "cv_rmse": None, "cv_press": None}
                              if task == "regression" else {"cv_mcc": None})
    result.update(oof_predicted=None, oof_probability=None)
    fold_ids = np.unique(folds[folds >= 0])
    if len(fold_ids) < 2 or (folds < 0).any():
        return result
    estimator = _build_estimator(
        algorithm, task, hyperparams or {}, feature_kind=feature_kind, corr_threshold=corr_threshold
    )
    predictions = np.empty(len(y_fit), dtype=float)
    probability = np.full(len(y_fit), np.nan) if task == "classification" else None
    scores = []
    for fold in fold_ids:
        test = folds == fold
        if task == "classification" and np.unique(y_fit[~test]).size < 2:
            return result
        fitted = clone(estimator).fit(x_matrix[~test], y_fit[~test])
        predicted = np.asarray(fitted.predict(x_matrix[test]))
        predictions[test] = predicted
        if probability is not None and hasattr(fitted, "predict_proba") and len(fitted.classes_) == 2:
            probability[test] = np.asarray(fitted.predict_proba(x_matrix[test]))[:, int(np.argmax(fitted.classes_))]
        scores.append(regression_metrics(y_fit[test], predicted)["r2"] if task == "regression"
                      else float(matthews_corrcoef(y_fit[test], predicted)))
    if not np.all(np.isfinite(predictions)):
        return result
    result["oof_predicted"] = predictions
    if probability is not None and np.isfinite(probability).all():
        result["oof_probability"] = probability
    mean_score = (float(np.mean(scores))
                  if all(score is not None and np.isfinite(score) for score in scores) else None)
    if task == "classification":
        result["cv_mcc"] = mean_score
        return result
    press = float(np.sum((y_fit - predictions) ** 2))
    tss = float(np.sum((y_fit - np.mean(y_fit)) ** 2))
    result.update(q2=float(1 - press / tss) if tss > 0 else None, cv_r2_mean=mean_score,
                  cv_rmse=float(np.sqrt(press / len(y_fit))), cv_press=press)
    return result


def y_scramble_score(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    folds: np.ndarray,
    algorithm: str,
    task: str,
    n_permutations: int = 10,
    hyperparams: dict[str, Any] | None = None,
    feature_kind: str = "rdkit2d",
    corr_threshold: float = 0.95,
    seed: int = 0,
) -> float | None:
    """OECD y-randomisation: shuffle labels `n_permutations` times and score each *out of sample*
    over the same ``folds``, returning mean pooled Q² (regression) or mean CV MCC (classification).
    Must use CV, not a train-fit score - a high-capacity model (RF) memorises shuffled labels
    in-sample and would always look bad. If the real model's CV score isn't clearly above this, its
    signal is chance. # ponytail: opt-in and O(n_permutations·folds·fit) — keep it small (~5-10)."""
    x_matrix = np.asarray(x_matrix, dtype=float)
    if int(n_permutations) < 1:
        return None
    rng = np.random.default_rng(int(seed))
    y_fit = y_vector.astype(float) if task == "regression" else y_vector.astype(int)
    scores: list[float] = []
    for _ in range(int(n_permutations)):
        shuffled = rng.permutation(y_fit)
        if task == "classification" and np.unique(shuffled).size < 2:
            continue
        metrics = cross_validation_metrics(
            x_matrix, shuffled, folds=folds, algorithm=algorithm, task=task,
            hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=corr_threshold,
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
    require_sklearn()  # the pickle holds sklearn estimators
    import joblib

    model = joblib.load(Path(path).expanduser().resolve())
    if not isinstance(model, FittedModel):
        raise ValueError(f"Artifact is not a FittedModel: {path}")
    return model


__all__ = [
    "FittedModel",
    "SUPPORTED_QSAR_ALGORITHMS",
    "classification_metrics",
    "cross_validation_metrics",
    "feature_importance",
    "fit_model",
    "load_model",
    "regression_metrics",
    "require_sklearn",
    "save_model",
    "supported_algorithms",
    "y_scramble_score",
]
