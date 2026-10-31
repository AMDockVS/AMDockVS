import numpy as np
import pytest

from amdockvs.qsar.modeling import (
    classification_metrics,
    cross_validation_metrics,
    domain_similarity,
    feature_importance,
    fit_model,
    load_model,
    regression_metrics,
    save_model,
    supported_algorithms,
    y_scramble_score,
)
from amdockvs.qsar.splitters import grouped_folds


def _linear_dataset(n=60, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    y = 2.0 * x[:, 0] - 1.0 * x[:, 1] + 0.5 * x[:, 2] + 0.1 * rng.normal(size=n)
    return x, y


def _blocks(n, k):
    """Contiguous folds, like an unshuffled KFold(k)."""
    return np.repeat(np.arange(k), int(np.ceil(n / k)))[:n]


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


def test_saved_model_loads_without_pymol_after_pymol_patched_pickle(tmp_path, monkeypatch):
    """PyMOL's chempy.pkl makes pickle._Pickler write Python 2 strings; the artifact must still
    load in a worker that never imported PyMOL."""
    import pickle
    import subprocess
    import sys

    pytest.importorskip("chempy.pkl")
    from joblib.numpy_pickle import NumpyPickler

    monkeypatch.setitem(NumpyPickler.dispatch, str, pickle._Pickler.dispatch[str])  # joblib after PyMOL
    x, y = _linear_dataset()
    model = fit_model(x, y, feature_names=("a", "b", "c"), algorithm="ridge", task="regression")
    path = save_model(tmp_path / "m.joblib", model)
    assert NumpyPickler.dispatch[str] is not pickle._Pickler.save_str  # left as it was found
    loaded = subprocess.run([sys.executable, "-c", "import sys, joblib; print(joblib.load(sys.argv[1]).feature_names)",
                             str(path)], capture_output=True, text=True)
    assert loaded.stdout.strip() == "('a', 'b', 'c')", loaded.stderr


def test_unknown_algorithm_rejected():
    x, y = _linear_dataset()
    with pytest.raises(ValueError):
        fit_model(x, y, feature_names=("a", "b", "c"), algorithm="xgboost_dnn", task="regression")


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


def test_cross_val_pooled_q2_and_oof_predictions():
    x, y = _linear_dataset()
    scores = cross_validation_metrics(x, y, folds=_blocks(len(y), 5), algorithm="ridge", task="regression")
    assert scores["q2"] > 0.9
    assert scores["oof_predicted"].shape == y.shape
    # fewer than two folds, or a row left out of every fold -> None, not a crash
    assert cross_validation_metrics(x, y, folds=np.zeros(len(y)), algorithm="ridge", task="regression")["q2"] is None
    partial = _blocks(len(y), 5)
    partial[0] = -1
    assert cross_validation_metrics(x, y, folds=partial, algorithm="ridge", task="regression")["q2"] is None


def test_classification_cv_returns_out_of_fold_probabilities():
    x, y = _linear_dataset(n=48)
    labels = (y >= np.median(y)).astype(int)
    folds = grouped_folds(list(range(48)), 4, seed=1)
    scores = cross_validation_metrics(x, labels, folds=folds, algorithm="logistic_regression",
                                      task="classification")
    assert scores["cv_mcc"] > 0.5
    assert ((scores["oof_probability"] >= 0) & (scores["oof_probability"] <= 1)).all()
    # a fold whose training part holds one class makes the CV undefined
    one_sided = np.where(labels == 1, 0, 1)
    assert cross_validation_metrics(x, labels, folds=one_sided, algorithm="logistic_regression",
                                    task="classification")["cv_mcc"] is None


def test_q2_matches_pooled_oof_predictions_and_differs_from_mean_fold_r2():
    x = np.arange(12).reshape(-1, 1)
    y = np.array([0., 1., 2., 3., 10., 11., 15., 20., 25., 30., 50., 90.])
    folds = grouped_folds(["a"] * 5 + ["b"] * 3 + ["c"] * 2 + ["d"] * 2, 3)
    predicted = np.empty_like(y)
    fold_r2 = []
    for fold in range(3):
        test = folds == fold
        predicted[test] = y[~test].mean()  # the baseline predicts the training mean
        fold_r2.append(1 - np.sum((y[test] - predicted[test]) ** 2) / np.sum((y[test] - y[test].mean()) ** 2))
    expected_press = np.sum((y - predicted) ** 2)
    scores = cross_validation_metrics(x, y, folds=folds, algorithm="baseline", task="regression",
                                      feature_kind="ecfp4")
    assert scores["q2"] == pytest.approx(1 - expected_press / np.sum((y - y.mean()) ** 2))
    assert scores["cv_r2_mean"] == pytest.approx(np.mean(fold_r2))
    assert scores["q2"] != pytest.approx(scores["cv_r2_mean"])
    assert scores["q2"] < 0  # poor models must retain negative predictivity
    assert scores["cv_press"] == pytest.approx(expected_press)
    assert scores["cv_rmse"] == pytest.approx(np.sqrt(expected_press / len(y)))
    assert scores["oof_predicted"] == pytest.approx(predicted)


@pytest.mark.parametrize("y,k", [([1., 1., 1., 2., 2., 2.], 2), ([0., 1., 4., 9.], 4)])
def test_q2_defined_even_when_individual_fold_r2_is_not(y, k):
    scores = cross_validation_metrics(np.arange(len(y)).reshape(-1, 1), np.array(y), folds=_blocks(len(y), k),
                                      algorithm="baseline", task="regression", feature_kind="ecfp4")
    assert np.isfinite(scores["q2"])
    assert scores["cv_r2_mean"] is None
    assert np.isfinite(scores["cv_rmse"])


def test_q2_is_undefined_for_a_constant_response():
    scores = cross_validation_metrics(np.arange(6).reshape(-1, 1), np.ones(6), folds=_blocks(6, 3),
                                      algorithm="baseline", task="regression", feature_kind="ecfp4")
    assert (scores["q2"], scores["cv_r2_mean"], scores["cv_rmse"], scores["cv_press"]) == (None, None, 0., 0.)


def test_scrambling_uses_the_same_pooled_q2_definition():
    x, y = _linear_dataset(n=15)
    folds = _blocks(15, 3)
    rng = np.random.default_rng(42)
    expected = []
    for _ in range(3):
        shuffled = rng.permutation(y)
        predicted = np.empty_like(shuffled)
        for fold in range(3):
            predicted[folds == fold] = shuffled[folds != fold].mean()
        expected.append(1 - np.sum((shuffled - predicted) ** 2) / np.sum((shuffled - shuffled.mean()) ** 2))
    actual = y_scramble_score(x, y, folds=folds, algorithm="baseline", task="regression",
                              n_permutations=3, seed=42, feature_kind="ecfp4")
    assert actual == pytest.approx(np.mean(expected))


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


def test_domain_similarity_is_mean_tanimoto_to_the_k_nearest():
    train = np.array([[1, 1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 1]])
    query = np.array([[1, 1, 0, 0], [0, 0, 0, 0]])
    # to [1,1,0,0]: 1, 0.5, 0 -> the two nearest average 0.75; an empty query is like nothing
    assert np.allclose(domain_similarity(train, query, k=2), [0.75, 0.0])
    assert np.allclose(domain_similarity(train, query, k=99), [0.5, 0.0])  # k caps at the set size
