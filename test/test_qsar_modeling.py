import numpy as np
import pytest

from amdockvs.qsar.modeling import (
    DEFAULT_QSAR_FEATURES,
    classification_metrics,
    cross_val_score_mean,
    cross_validation_metrics,
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


def test_grouped_split_rejects_one_scaffold_and_keeps_training_group():
    with pytest.raises(ValueError, match="at least two distinct scaffold groups"):
        grouped_holdout_split(["same"] * 10, test_size=.3)
    train, test = grouped_holdout_split(["a"] * 5 + ["b"] * 5, test_size=.99)
    assert len(train) == len(test) == 5
    train, test = grouped_holdout_split(["a"] * 5 + ["b"] * 5, test_size=.001)
    assert len(train) == len(test) == 5


def test_binary_probability_uses_estimator_class_order_and_is_not_confidence():
    from types import SimpleNamespace
    from amdockvs.qsar.modeling import FittedModel

    # Exercise reversed class order and confident negatives, including SVC-style disagreement
    # between predict() and argmax(predict_proba()).
    estimator = SimpleNamespace(classes_=np.array([1, 0]),
        predict_proba=lambda x: np.array([[.2, .8], [.997, .003]]))
    fitted = FittedModel(estimator=estimator, task="classification", algorithm="svc", feature_names=("a",))
    assert fitted.predict_positive_probability(np.array([[0], [1]])) == pytest.approx([.2, .997])
    assert fitted.predict_confidence(np.array([[0], [1]])) == pytest.approx([.8, .997])


def test_cross_val_mean_and_pooled_q2():
    x, y = _linear_dataset()
    q2 = cross_validation_metrics(x, y, algorithm="ridge", task="regression", cv=5)["q2"]
    assert q2 is not None and q2 > 0.9
    # cv too large for the sample count -> None, not a crash
    assert cross_val_score_mean(x[:3], y[:3], algorithm="ridge", task="regression", cv=5) is None


@pytest.mark.parametrize("task,algorithm", [("regression", "ridge"), ("classification", "logistic_regression")])
def test_scaffold_cv_keeps_groups_disjoint(monkeypatch, task, algorithm):
    import sklearn.model_selection as selection

    x, y = _linear_dataset(n=48)
    groups = np.tile(np.arange(6), 8)  # interleave scaffolds so row-wise folds would leak
    if task == "classification":
        y = np.repeat(np.arange(8) % 2, 6)
    original = selection.GroupKFold.split
    observed = []

    def check_folds(self, x, y=None, groups=None):
        for train, test in original(self, x, y, groups):
            assert set(groups[train]).isdisjoint(groups[test])
            observed.append((train, test))
            yield train, test

    monkeypatch.setattr(selection.GroupKFold, "split", check_folds)
    score = cross_val_score_mean(x, y, algorithm=algorithm, task=task, cv=3, groups=groups)
    assert score is not None and np.isfinite(score)
    assert len(observed) == 3


@pytest.mark.parametrize("grouped", [False, True])
def test_q2_matches_pooled_oof_predictions_and_differs_from_mean_fold_r2(grouped):
    from sklearn.dummy import DummyRegressor
    from sklearn.metrics import r2_score
    from sklearn.model_selection import GroupKFold, KFold, cross_val_predict

    x = np.arange(12).reshape(-1, 1)
    y = np.array([0., 1., 2., 3., 10., 11., 15., 20., 25., 30., 50., 90.])
    groups = np.array(["a"] * 5 + ["b"] * 3 + ["c"] * 2 + ["d"] * 2) if grouped else None
    splitter = GroupKFold(3) if grouped else KFold(3)
    splits = list(splitter.split(x, y, groups))
    predicted = cross_val_predict(DummyRegressor(strategy="mean"), x, y, cv=splits)
    expected_press = np.sum((y - predicted) ** 2)
    expected_q2 = 1 - expected_press / np.sum((y - y.mean()) ** 2)
    expected_mean = np.mean([r2_score(y[test], predicted[test]) for _, test in splits])
    scores = cross_validation_metrics(x, y, algorithm="baseline", task="regression", cv=3,
                                      feature_kind="ecfp4", groups=groups)
    assert scores["q2"] == pytest.approx(expected_q2)
    assert scores["cv_r2_mean"] == pytest.approx(expected_mean)
    assert scores["q2"] != pytest.approx(scores["cv_r2_mean"])
    assert scores["q2"] < 0  # poor models must retain negative predictivity
    assert scores["cv_press"] == pytest.approx(expected_press)
    assert scores["cv_rmse"] == pytest.approx(np.sqrt(expected_press / len(y)))


@pytest.mark.parametrize("y,folds", [([1., 1., 1., 2., 2., 2.], 2), ([0., 1., 4., 9.], 4)])
def test_q2_defined_even_when_individual_fold_r2_is_not(y, folds):
    scores = cross_validation_metrics(np.arange(len(y)).reshape(-1, 1), np.array(y),
        algorithm="baseline", task="regression", cv=folds, feature_kind="ecfp4")
    assert np.isfinite(scores["q2"])
    assert scores["cv_r2_mean"] is None
    assert np.isfinite(scores["cv_rmse"])


def test_q2_is_undefined_for_a_constant_response():
    scores = cross_validation_metrics(np.arange(6).reshape(-1, 1), np.ones(6),
        algorithm="baseline", task="regression", cv=3, feature_kind="ecfp4")
    assert scores == {"q2": None, "cv_r2_mean": None, "cv_rmse": 0., "cv_press": 0.}


def test_scrambling_uses_the_same_pooled_q2_definition():
    from sklearn.dummy import DummyRegressor
    from sklearn.model_selection import KFold, cross_val_predict
    from amdockvs.qsar.modeling import y_scramble_score

    x, y = _linear_dataset(n=15)
    rng = np.random.default_rng(42)
    expected = []
    for _ in range(3):
        shuffled = rng.permutation(y)
        predicted = cross_val_predict(DummyRegressor(strategy="mean"), x, shuffled, cv=KFold(3))
        expected.append(1 - np.sum((shuffled - predicted) ** 2) / np.sum((shuffled - shuffled.mean()) ** 2))
    actual = y_scramble_score(x, y, algorithm="baseline", task="regression", cv=3,
                              n_permutations=3, seed=42, feature_kind="ecfp4")
    assert actual == pytest.approx(np.mean(expected))


def test_scaffold_cv_does_not_fall_back_when_groups_are_insufficient():
    x, y = _linear_dataset(n=12)
    assert cross_val_score_mean(x, y, algorithm="ridge", task="regression", cv=3,
                                groups=["one"] * 6 + ["two"] * 6) is None
    with pytest.raises(ValueError, match="groups"):
        cross_val_score_mean(x, y, algorithm="ridge", task="regression", cv=3, groups=["one"])


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


def test_linear_models_expose_signed_weights():
    # a linear model has no feature_importances_; its (scaled) coefficients are the weights
    rng = np.random.default_rng(0)
    x = rng.random((40, 4))
    y = 3.0 * x[:, 0] - 2.0 * x[:, 1]
    model = fit_model(x, y, feature_names=("up", "down", "noise_a", "noise_b"), algorithm="ridge", task="regression")
    weights = dict(feature_importance(model.estimator, model.feature_names))
    assert weights["up"] > 0 > weights["down"]
    assert abs(weights["noise_a"]) < abs(weights["down"])
