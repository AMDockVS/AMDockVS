import numpy as np
import pytest

from amdockvs.qsar.modeling import (
    DEFAULT_QSAR_FEATURES,
    classification_metrics,
    cross_val_score_mean,
    feature_importance,
    fit_model,
    grouped_holdout_split,
    load_model,
    normalize_feature_names,
    regression_metrics,
    save_model,
    supported_algorithms,
)


def _linear_dataset(n=60, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    y = 2.0 * x[:, 0] - 1.0 * x[:, 1] + 0.5 * x[:, 2] + 0.1 * rng.normal(size=n)
    return x, y


def test_normalize_feature_names_validates():
    assert normalize_feature_names(None) == DEFAULT_QSAR_FEATURES
    assert normalize_feature_names(["mw", "logp"]) == ("mw", "logp")
    with pytest.raises(ValueError):
        normalize_feature_names(["not_a_feature"])


@pytest.mark.parametrize("algo", supported_algorithms("regression"))
def test_regression_templates_fit_and_predict(algo):
    x, y = _linear_dataset()
    model = fit_model(x, y, feature_names=("a", "b", "c"), algorithm=algo, task="regression")
    preds = model.predict(x)
    assert preds.shape == (x.shape[0],)
    # linear models should recover the signal nearly perfectly
    if algo in {"linear_regression", "ridge"}:
        assert regression_metrics(y, preds)["r2"] > 0.95


def test_classification_template_and_metrics():
    x, y = _linear_dataset()
    labels = (y >= np.median(y)).astype(int)
    model = fit_model(x, labels, feature_names=("a", "b", "c"), algorithm="random_forest", task="classification")
    metrics = classification_metrics(labels, model.predict(x))
    assert metrics["accuracy"] > 0.9
    conf = model.predict_confidence(x)
    assert conf is not None and conf.shape == (x.shape[0],)


def test_save_load_roundtrip(tmp_path):
    x, y = _linear_dataset()
    model = fit_model(x, y, feature_names=("a", "b", "c"), algorithm="ridge", task="regression")
    path = save_model(tmp_path / "m.joblib", model)
    reloaded = load_model(path)
    assert reloaded.feature_names == ("a", "b", "c")
    assert np.allclose(reloaded.predict(x), model.predict(x))


def test_unknown_algorithm_rejected():
    x, y = _linear_dataset()
    with pytest.raises(ValueError):
        fit_model(x, y, feature_names=("a", "b", "c"), algorithm="xgboost_dnn", task="regression")


def test_grouped_split_keeps_scaffolds_disjoint():
    groups = ["s1", "s1", "s2", "s2", "s3", "s3", "s4", "s4", "s5", "s5"]
    train_idx, test_idx = grouped_holdout_split(groups, test_size=0.3, seed=7)
    assert set(train_idx).isdisjoint(test_idx)
    assert {groups[i] for i in train_idx}.isdisjoint({groups[i] for i in test_idx})
    assert test_idx and train_idx  # both non-empty for this size


def test_cross_val_and_q2():
    x, y = _linear_dataset()
    q2 = cross_val_score_mean(x, y, algorithm="ridge", task="regression", cv=5)
    assert q2 is not None and q2 > 0.9
    # cv too large for the sample count -> None, not a crash
    assert cross_val_score_mean(x[:3], y[:3], algorithm="ridge", task="regression", cv=5) is None


def test_classification_metrics_roc_auc_and_mcc():
    y_true = [0, 0, 1, 1]
    y_pred = [0, 1, 1, 1]
    scores = [0.1, 0.4, 0.8, 0.9]
    metrics = classification_metrics(y_true, y_pred, scores)
    assert metrics["roc_auc"] == 1.0  # scores perfectly rank the classes
    assert -1.0 <= metrics["mcc"] <= 1.0


def test_ecfp4_feature_kind_roundtrips(tmp_path):
    # a fingerprint-style 0/1 matrix; feature_kind/fp metadata survive save/load
    rng = np.random.default_rng(0)
    x = (rng.random((20, 16)) > 0.5).astype(float)
    y = x[:, 0] + x[:, 1]
    model = fit_model(
        x, y, feature_names=tuple(f"bit_{i}" for i in range(16)), algorithm="random_forest",
        task="regression", feature_kind="ecfp4", fp_radius=2, fp_nbits=16,
    )
    assert model.feature_kind == "ecfp4" and model.fp_nbits == 16
    reloaded = load_model(save_model(tmp_path / "fp.joblib", model))
    assert reloaded.feature_kind == "ecfp4" and reloaded.fp_radius == 2
    imp = feature_importance(reloaded.estimator, reloaded.feature_names, top_n=3)
    assert imp and imp[0][0].startswith("bit_")
