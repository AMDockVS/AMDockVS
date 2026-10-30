"""Keep outer test molecules out of internal validation and label-permutation checks."""
import csv

import numpy as np
import pytest

from amdockvs.runtime import AMDockVSRuntime
from amdockvs.qsar import modeling


@pytest.fixture
def qsar_runtime(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock_home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite_home"))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="validation", folder=tmp_path / "project")
        series = ("{}c1ccccc1", "{}c1ccncc1", "{}C(=O)NC1CCCCC1",
                  "{}Oc1ccc2[nH]ccc2c1", "{}N1CCCCC1", "{}c1cccs1")
        rows = [(f"s{s}_{n}", template.format("C" * n), n)
                for s, template in enumerate(series) for n in range(1, 9)]
        library = tmp_path / "library.smi"
        library.write_text("".join(f"{smiles} {name}\n" for name, smiles, _ in rows))
        jobs = runtime.loader.load_ligands([library], executor_name="thread")
        jobs = runtime.wait_for_jobs(list(jobs), timeout_s=60)
        assert all(job.status == "completed" for job in jobs.values())
        job = runtime.qsar.compute_descriptors(executor_name="thread")
        assert runtime.wait_for_jobs([job], timeout_s=60)[job].status == "completed"
        for endpoint in ("regression", "classification"):
            activities = tmp_path / f"{endpoint}.csv"
            with activities.open("w") as handle:
                writer = csv.writer(handle)
                writer.writerow(("smiles", "value"))
                writer.writerows((smiles, n if endpoint == "regression" else int(n >= 5))
                                 for _, smiles, n in rows)
            runtime.qsar.load_activities(activities, endpoint=endpoint, match_by="smiles",
                                        kind="categorical" if endpoint == "classification" else "continuous")
        yield runtime
    finally:
        runtime.shutdown()


def test_binary_diagnostics_match_sklearn_on_each_subset(qsar_runtime):
    from sklearn.metrics import average_precision_score, confusion_matrix, precision_recall_curve, roc_auc_score

    api = qsar_runtime.qsar
    model = api.train(endpoint="classification", task="classification", algorithm="logistic_regression",
                      split="scaffold", test_size=.25, seed=42)
    api.predict(model=model.id)
    predictions = {p.molecule_id: p for p in api.list_predictions(model=model.id)}
    activities = {r["molecule_id"]: int(r["value"]) for r in api.activity_rows(endpoint="classification")}
    assignments = api.model_subsets(model=model.id)
    for subset, n in (("all", 48), ("train", 32), ("test", 16)):
        ids = [mid for mid in predictions if subset == "all" or assignments.get(mid) == subset]
        y = [activities[mid] for mid in ids]
        scores = [predictions[mid].positive_probability for mid in ids]
        labels = [predictions[mid].value for mid in ids]
        cm = api.confusion_matrix_data(model=model.id, subset=subset)
        pr = api.precision_recall_curve_points(model=model.id, subset=subset)
        roc = api.roc_curve_points(model=model.id, subset=subset)
        assert cm["n"] == pr["n"] == roc["n"] == n
        assert cm["labels"] == [0, 1]
        np.testing.assert_array_equal(cm["counts"], confusion_matrix(y, labels, labels=[0, 1]))
        precision, recall, _ = precision_recall_curve(y, scores)
        np.testing.assert_allclose(pr["points"], np.column_stack((recall, precision)))
        assert pr["average_precision"] == pytest.approx(average_precision_score(y, scores))
        assert pr["prevalence"] == pytest.approx(np.mean(y))
        assert roc["auc"] == pytest.approx(roc_auc_score(y, scores))


def test_single_class_subset_keeps_matrix_but_rejects_curves(qsar_runtime):
    api = qsar_runtime.qsar
    # A real threshold on a continuous endpoint must never fall back to raw values.
    model = api.train(endpoint="regression", task="classification", class_threshold=4.5,
                      algorithm="logistic_regression", test_size=.25, seed=42)
    ids = [mid for mid, subset in model.metrics["split_assignment"] if subset == "test"]
    for mid in ids:
        api.set_activity(ligand_id=mid, endpoint="regression", value=8.)
    result = api.confusion_matrix_data(model=model.id, subset="test")
    assert sum(result["counts"][0]) == 0
    assert sum(result["counts"][1]) == len(ids)
    for method in (api.roc_curve_points, api.precision_recall_curve_points):
        with pytest.raises(ValueError, match="both classes in the selected subset"):
            method(model=model.id, subset="test")



def test_stale_threshold_on_a_categorical_endpoint_uses_the_raw_labels(qsar_runtime):
    from amdockvs.models import QSARModelRecord

    api = qsar_runtime.qsar
    model = api.train(endpoint="classification", task="classification", algorithm="logistic_regression",
                      test_size=.25, seed=42)
    expected = api.roc_curve_points(model=model.id)["auc"]
    with qsar_runtime.workspace.project_db.get_session() as session:
        record = session.get(QSARModelRecord, model.id)
        record.metrics = {**record.metrics, "class_threshold": 0.0}  # collapses 0/1 labels to all 1
        session.add(record)
        session.commit()
    assert api.roc_curve_points(model=model.id)["auc"] == pytest.approx(expected)

@pytest.mark.parametrize("task,algorithm", [("regression", "ridge"), ("classification", "logistic_regression")])
def test_diagnostics_never_fall_back_to_all_without_test_split(qsar_runtime, task, algorithm):
    api = qsar_runtime.qsar
    model = api.train(endpoint=task, task=task, algorithm=algorithm, test_size=0.)
    methods = ([api.observed_predicted_points] if task == "regression" else
               [api.confusion_matrix_data, api.roc_curve_points, api.precision_recall_curve_points])
    for method in methods:
        with pytest.raises(ValueError, match="'test' subset"):
            method(model=model.id, subset="test")
        assert method(model=model.id, subset="train")["n"] == 48


def test_regression_diagnostic_points_match_predictions(qsar_runtime):
    api = qsar_runtime.qsar
    model = api.train(endpoint="regression", algorithm="ridge", test_size=.25, seed=42)
    api.predict(model=model.id)
    predictions = {p.molecule_id: p.value for p in api.list_predictions(model=model.id)}
    activities = {r["molecule_id"]: r["value"] for r in api.activity_rows(endpoint="regression")}
    assignments = api.model_subsets(model=model.id)
    result = api.observed_predicted_points(model=model.id, subset="test")
    assert result["n"] == 12
    np.testing.assert_allclose(sorted(result["points"]), sorted(
        (activities[mid], prediction) for mid, prediction in predictions.items() if assignments[mid] == "test"))


@pytest.mark.parametrize("split", ["random", "scaffold"])
@pytest.mark.parametrize("task,algorithm", [("regression", "ridge"), ("classification", "logistic_regression")])
def test_internal_validation_uses_only_training_molecules(qsar_runtime, monkeypatch, split, task, algorithm):
    calls = []
    original = modeling.cross_validation_metrics

    def capture(x, y, **kwargs):
        calls.append((np.array(x), kwargs.get("groups")))
        return original(x, y, **kwargs)

    monkeypatch.setattr(modeling, "cross_validation_metrics", capture)
    model = qsar_runtime.qsar.train(endpoint=task, algorithm=algorithm, task=task, split=split,
        test_size=.25, cv_folds=3, y_scramble=2, seed=42, feature_names=("mw", "logp", "tpsa"))
    assignment = dict(model.metrics["split_assignment"])
    train_ids = {mid for mid, subset in assignment.items() if subset == "train"}
    records = list(qsar_runtime.molecules.stream(qsar_runtime.molecules.select(role="ligand")))
    expected = sorted((r.mw, r.logp, r.tpsa) for r in records if r.id in train_ids)
    assert len(train_ids) < 48
    assert model.metrics["cv_samples"] == len(train_ids)
    assert model.metrics["y_scramble_samples"] == len(train_ids)
    assert model.metrics["cv_split"] == ("scaffold" if split == "scaffold" else "row")
    assert len(calls) == 3  # CV and two shuffled-label refits
    keys = qsar_runtime.qsar._scaffold_keys(records)
    for x, groups in calls:
        assert sorted(map(tuple, x)) == expected
        if split == "scaffold":
            group_by_mw = {r.mw: keys[r.id] for r in records}
            assert list(groups) == [group_by_mw[mw] for mw in x[:, 0]]
        else:
            assert groups is None


@pytest.mark.parametrize("split", ["random", "scaffold"])
def test_changing_test_labels_does_not_change_internal_validation(qsar_runtime, split):
    api = qsar_runtime.qsar
    params = dict(endpoint="regression", algorithm="ridge", split=split, test_size=.25,
                  cv_folds=3, y_scramble=2, seed=42, feature_names=("mw", "logp", "tpsa"))
    first = api.train(**params)
    test_ids = {mid for mid, subset in first.metrics["split_assignment"] if subset == "test"}
    assert test_ids
    for mid in test_ids:
        api.set_activity(ligand_id=mid, endpoint="regression", value=1000.0 + mid)
    second = api.train(**params)
    assert second.metrics["split_assignment"] == first.metrics["split_assignment"]
    for metric in ("q2", "y_scramble"):
        assert first.metrics[metric] is not None
        assert second.metrics[metric] == pytest.approx(first.metrics[metric], abs=1e-12)
    assert second.metrics["test"]["rmse"] > first.metrics["test"]["rmse"] + 100


def test_insufficient_training_scaffolds_report_no_cv_score(qsar_runtime):
    model = qsar_runtime.qsar.train(endpoint="regression", algorithm="ridge", split="scaffold",
                                   test_size=.25, cv_folds=5, y_scramble=1, seed=42)
    assert model.metrics["q2"] is None
    assert "requested 5 folds" in model.metrics["cv_warning"]
    assert "only 4 scaffold group(s)" in model.metrics["cv_warning"]
    assert model.metrics["y_scramble"] is None
    assert "y_scramble_warning" in model.metrics
    assert "test" in model.metrics


def test_without_a_holdout_all_molecules_are_training_data(qsar_runtime):
    model = qsar_runtime.qsar.train(endpoint="regression", algorithm="ridge", split="scaffold",
                                   test_size=0, cv_folds=3, seed=42)
    assert model.metrics["n_train"] == model.metrics["cv_samples"] == 48
    assert model.metrics["q2"] is not None
    assert "test" not in model.metrics


def test_saved_q2_matches_independent_training_only_oof_predictions(qsar_runtime):
    from sklearn.metrics import r2_score
    from sklearn.model_selection import GroupKFold, cross_val_predict

    api = qsar_runtime.qsar
    features = ("mw", "logp", "tpsa")
    model = api.train(endpoint="regression", algorithm="ridge", split="scaffold", test_size=.25,
                      cv_folds=3, y_scramble=1, feature_names=features)
    train_ids = {mid for mid, subset in model.metrics["split_assignment"] if subset == "train"}
    training = [r for r in qsar_runtime.molecules.stream(qsar_runtime.molecules.select(role="ligand"))
                if r.id in train_ids]
    with qsar_runtime.workspace.project_db.get_session() as session:
        _, fitted = api._load_fitted(session, model)
        activities = api._latest_activity_by_ligand(session, "regression")
    x = np.array([[getattr(r, f) for f in features] for r in training])
    y = np.array([activities[r.id] for r in training])
    scaffolds = api._scaffold_keys(training)
    groups = [scaffolds[r.id] for r in training]
    predicted = cross_val_predict(fitted.estimator, x, y, cv=GroupKFold(3), groups=groups)
    assert model.metrics["q2"] == pytest.approx(r2_score(y, predicted))
    assert model.metrics["cv_rmse"] == pytest.approx(np.sqrt(np.mean((y - predicted) ** 2)))
    assert model.metrics["q2_method"] == "pooled_oof"
    assert model.metrics["y_scramble_metric"] == "q2_pooled_oof"
    assert model.metrics["cv_samples"] == len(training) < 48




def test_single_scaffold_holdout_fails_without_saving_a_model(qsar_runtime):
    from amdockvs.molecules.scopes import MoleculeScope

    api = qsar_runtime.qsar
    records = list(qsar_runtime.molecules.stream(qsar_runtime.molecules.select(role="ligand")))
    ids = [r.id for r in records if r.name.startswith("s0_")]
    scope = MoleculeScope(filters={"id__in": ids})
    before = len(api.list_models())
    with pytest.raises(ValueError, match="at least two distinct scaffold groups"):
        api.train(endpoint="regression", algorithm="ridge", molecule_set=scope,
                  split="scaffold", test_size=.3)
    assert len(api.list_models()) == before
    with pytest.raises(ValueError, match="at least two distinct scaffold groups"):
        api.split_preview(endpoint="regression", molecule_set=scope, split="scaffold", test_size=.3)
    # An explicitly requested no-holdout fit still works.
    model = api.train(endpoint="regression", algorithm="ridge", molecule_set=scope,
                      split="scaffold", test_size=0)
    assert model.metrics["n_train"] == 8
    assert "test" not in model.metrics


@pytest.mark.parametrize("task,algorithm", [("regression", "ridge"), ("classification", "logistic_regression")])
def test_evaluate_respects_recorded_subsets(qsar_runtime, task, algorithm):
    api = qsar_runtime.qsar
    model = api.train(endpoint=task, algorithm=algorithm, task=task, split="scaffold", test_size=.25)
    for subset in ("train", "test"):
        actual = api.evaluate(model=model, subset=subset)
        assert actual["subset"] == subset
        for metric, value in model.metrics[subset].items():
            assert actual[metric] == pytest.approx(value) if value is not None else actual[metric] is None
    assert api.evaluate(model=model)["n_samples"] == 48
    with pytest.raises(ValueError, match="subset must be"):
        api.evaluate(model=model, subset="typo")
    no_holdout = api.train(endpoint=task, algorithm=algorithm, task=task, test_size=0)
    with pytest.raises(ValueError, match="'test' subset"):
        api.evaluate(model=no_holdout, subset="test")


def test_binary_prediction_ranking_uses_positive_probability(qsar_runtime):
    api = qsar_runtime.qsar
    model = api.train(endpoint="classification", algorithm="logistic_regression", task="classification")
    api.predict(model=model)
    rows = api.prediction_rows(model=model)
    assert all(r["positive_probability"] is not None for r in rows)
    assert any(r["value"] == 0 and r["confidence"] > .8 and r["positive_probability"] < .2 for r in rows)
    for higher in (True, False):
        expected = sorted(rows, key=lambda r: ((-1 if higher else 1) * r["positive_probability"], r["molecule_id"]))
        cut = api.cut_predictions(model=model, top_n=5, higher_is_better=higher, ids_limit=5)
        assert cut["ids"] == [r["molecule_id"] for r in expected[:5]]
    cutoff = .8
    cut = api.cut_predictions(model=model, cutoff=cutoff, ids_limit=48)
    assert set(cut["ids"]) == {r["molecule_id"] for r in rows if r["positive_probability"] >= cutoff}
    # Old confidence/labels cannot stand in for the missing probability.
    from amdockvs.models import QSARPredictionRecord
    with qsar_runtime.workspace.project_db.get_session() as session:
        from sqlmodel import select
        row = session.exec(select(QSARPredictionRecord).where(QSARPredictionRecord.model_id == model.id)).first()
        row.positive_probability = None
        session.add(row)
        session.commit()
    with pytest.raises(ValueError, match="Run prediction again"):
        api.cut_predictions(model=model, top_n=1)
    api.predict(model=model)
    assert api.cut_predictions(model=model, top_n=1)["kept"] == 1


def test_probability_column_added_when_reopening_an_older_project(qsar_runtime):
    from amdockvs.models import QSARPredictionRecord
    from sqlmodel import select

    runtime = qsar_runtime
    model = runtime.qsar.train(endpoint="regression", algorithm="ridge")
    runtime.qsar.predict(model=model)
    project_id = runtime.get_active_project().id
    with runtime.workspace.project_db.get_session() as session:
        session.connection().exec_driver_sql("ALTER TABLE qsar_predictions DROP COLUMN positive_probability")
        session.commit()
    runtime.close_project()
    runtime.open_project(project_id)
    with runtime.workspace.project_db.get_session() as session:
        rows = session.exec(select(QSARPredictionRecord)).all()
        assert len(rows) == 48
        assert all(r.positive_probability is None for r in rows)




@pytest.mark.parametrize("source,expected", [("descriptors", "descriptors_2d"),
                                            ("rdkit2d", "descriptors_2d"),
                                            ("ecfp4", "fingerprints_ecfp4")])
def test_trained_and_external_models_record_their_feature_type(qsar_runtime, source, expected):
    from amdockvs.core.vocab import CODES

    runtime = qsar_runtime
    if source == "ecfp4":
        job = runtime.qsar.compute_fingerprints(executor_name="thread")
        assert runtime.wait_for_jobs([job], timeout_s=60)[job].status == "completed"
    model = runtime.qsar.train(endpoint="regression", algorithm="ridge", feature_source=source)
    assert model.feature_type == expected
    assert model.metrics["feature_kind"] == source
    path = runtime.get_project_paths()["project_root"] / model.model_path
    external = runtime.qsar.load_external_model(path=path, name=f"external_{source}")
    assert external.feature_type == expected
    assert external.metrics["feature_kind"] == source
    assert runtime.qsar.predict(model=external)["predicted"] == 48
    with runtime.workspace.project_db.get_session() as session:
        code = session.connection().exec_driver_sql(
            "SELECT feature_type FROM qsar_models WHERE id = ?", (model.id,)).scalar()
        assert code == CODES["feature_type"][expected]
    assert CODES["feature_type"]["descriptors_2d"] == 1501  # existing codes must never move


def test_existing_ecfp4_metadata_is_corrected_on_open(qsar_runtime):
    from amdockvs.models import QSARModelRecord

    runtime = qsar_runtime
    job = runtime.qsar.compute_fingerprints(executor_name="thread")
    assert runtime.wait_for_jobs([job], timeout_s=60)[job].status == "completed"
    model = runtime.qsar.train(endpoint="regression", algorithm="ridge", feature_source="ecfp4")
    descriptor = runtime.qsar.train(endpoint="regression", algorithm="ridge")
    path = runtime.get_project_paths()["project_root"] / model.model_path
    original_artifact = path.read_bytes()
    original_metrics = dict(model.metrics)
    project_id = runtime.get_active_project().id
    with runtime.workspace.project_db.get_session() as session:
        old = session.get(QSARModelRecord, model.id)
        old.feature_type = "descriptors_2d"
        session.add(old)
        session.flush()
        # The code was absent in projects created before this correction.
        session.connection().exec_driver_sql(
            "DELETE FROM codes WHERE domain = 'feature_type' AND name = 'fingerprints_ecfp4'")
        session.commit()
    for _ in range(2):
        runtime.close_project()
        runtime.open_project(project_id)
        models = {m.id: m for m in runtime.qsar.list_models()}
        assert models[model.id].feature_type == "fingerprints_ecfp4"
        assert models[model.id].metrics == original_metrics
        assert models[descriptor.id].feature_type == "descriptors_2d"
        assert path.read_bytes() == original_artifact
