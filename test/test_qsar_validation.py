"""Keep outer test molecules out of internal validation; diagnostics read the stored members."""
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


def _members(runtime, model, subset="all"):
    return runtime.qsar.model_members(model=model, subset=subset)[1]


def _series(runtime):
    """{molecule id: its series}: each series of the fixture shares one Murcko scaffold."""
    records = runtime.molecules.stream(runtime.molecules.select(role="ligand"))
    return {r.id: r.name.split("_")[0] for r in records}


def test_single_class_subset_keeps_matrix_but_rejects_curves(qsar_runtime):
    from amdockvs.core.constants import TABLE_QSAR_MODEL_MEMBERS

    api = qsar_runtime.qsar
    # A real threshold on a continuous endpoint must never fall back to raw values.
    model = api.train(endpoint="regression", task="classification", class_threshold=4.5,
                      algorithm="logistic_regression", test_size=.25, seed=42)
    with qsar_runtime.workspace.project_db.get_session() as session:
        session.connection().exec_driver_sql(
            f"UPDATE {TABLE_QSAR_MODEL_MEMBERS} SET observed = 1 WHERE model_id = ? AND role = 'test'", (model.id,))
        session.commit()
    n_test = model.n_test
    result = api.confusion_matrix_data(model=model.id, subset="test")
    assert sum(result["counts"][0]) == 0
    assert sum(result["counts"][1]) == n_test
    for method in (api.roc_curve_points, api.precision_recall_curve_points):
        with pytest.raises(ValueError, match="both classes in the selected subset"):
            method(model=model.id, subset="test")


def test_diagnostics_keep_the_labels_the_model_was_built_on(qsar_runtime):
    api = qsar_runtime.qsar
    model = api.train(endpoint="regression", algorithm="ridge", test_size=.25, seed=42)
    before = api.observed_predicted_points(model=model.id, subset="test")
    for member in _members(qsar_runtime, model, "test"):
        api.set_activity(ligand_id=member.molecule_id, endpoint="regression", value=1000.)
    assert api.observed_predicted_points(model=model.id, subset="test")["points"] == before["points"]


def test_registry_columns_and_members_describe_the_model(qsar_runtime):
    api = qsar_runtime.qsar
    model = api.train(endpoint="classification", task="classification", algorithm="logistic_regression",
                      split="scaffold", test_size=.25, cv_folds=3, seed=42)
    assert (model.task, model.engine, model.splitter) == ("classification", "sklearn", "scaffold")
    assert (model.n_train, model.n_test) == (32, 16)
    assert model.test_mcc == pytest.approx(model.metrics["test"]["mcc"])
    assert model.test_roc_auc == pytest.approx(model.metrics["test"]["roc_auc"])
    assert model.cv_mcc == pytest.approx(model.metrics["cv_mcc"])
    assert model.test_r2 is None and model.q2 is None
    assert "split_assignment" not in model.metrics
    members = _members(qsar_runtime, model)
    assert len(members) == 48
    train = [m for m in members if m.role == "train"]
    assert all(m.fold is None and m.oof_predicted is None for m in members if m.role == "test")
    assert {m.fold for m in train} == {0, 1, 2}
    assert all(m.oof_predicted is not None and 0 <= m.oof_probability <= 1 for m in train)
    assert api.roc_curve_points(model=model.id, subset="cv")["n"] == 32
    assert set(api.model_subsets(model=model.id).values()) == {"train", "test"}


def test_deleting_a_molecule_removes_its_membership(qsar_runtime):
    api = qsar_runtime.qsar
    model = api.train(endpoint="regression", algorithm="ridge", test_size=.25, seed=42)
    gone = _members(qsar_runtime, model)[0].molecule_id
    qsar_runtime.molecules.delete([gone])
    assert gone not in {m.molecule_id for m in _members(qsar_runtime, model)}


def test_reopening_a_project_with_the_old_qsar_tables_drops_their_models(qsar_runtime):
    from amdockvs.core.constants import TABLE_QSAR_MODELS

    runtime = qsar_runtime
    model = runtime.qsar.train(endpoint="regression", algorithm="ridge")
    runtime.qsar.predict(model=model)
    project_id = runtime.get_active_project().id
    with runtime.workspace.project_db.get_session() as session:
        session.connection().exec_driver_sql(f"ALTER TABLE {TABLE_QSAR_MODELS} DROP COLUMN task")
        session.commit()
    runtime.close_project()
    runtime.open_project(project_id)
    assert runtime.qsar.list_models() == []
    assert runtime.qsar.train(endpoint="regression", algorithm="ridge").task == "regression"


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
        calls.append((len(x), np.array(kwargs["folds"])))
        return original(x, y, **kwargs)

    monkeypatch.setattr(modeling, "cross_validation_metrics", capture)
    model = qsar_runtime.qsar.train(endpoint=task, algorithm=algorithm, task=task, split=split,
                                    test_size=.25, cv_folds=3, y_scramble=2, seed=42)
    train = _members(qsar_runtime, model, "train")
    assert len(train) == model.n_train < 48
    assert len(calls) == 3  # CV and two shuffled-label refits, all on the same training folds
    folds = sorted(m.fold for m in train)
    for n, call_folds in calls:
        assert n == len(train)
        assert sorted(call_folds) == folds
    if split == "scaffold":
        series = _series(qsar_runtime)
        by_fold = {}
        for m in train:
            by_fold.setdefault(m.fold, set()).add(series[m.molecule_id])
        assert sum(len(s) for s in by_fold.values()) == len(set().union(*by_fold.values()))


@pytest.mark.parametrize("split", ["random", "scaffold"])
def test_changing_test_labels_does_not_change_internal_validation(qsar_runtime, split):
    api = qsar_runtime.qsar
    params = dict(endpoint="regression", algorithm="ridge", split=split, test_size=.25,
                  cv_folds=3, y_scramble=2, seed=42)
    first = api.train(**params)
    roles = {m.molecule_id: (m.role, m.fold) for m in _members(qsar_runtime, first)}
    for mid, (role, _fold) in roles.items():
        if role == "test":
            api.set_activity(ligand_id=mid, endpoint="regression", value=1000.0 + mid)
    second = api.train(**params)
    assert {m.molecule_id: (m.role, m.fold) for m in _members(qsar_runtime, second)} == roles
    for metric in ("q2", "y_scramble"):
        assert first.metrics[metric] is not None
        assert second.metrics[metric] == pytest.approx(first.metrics[metric], abs=1e-12)
    assert second.metrics["test"]["rmse"] > first.metrics["test"]["rmse"] + 100


def test_insufficient_training_scaffolds_report_no_cv_score(qsar_runtime):
    model = qsar_runtime.qsar.train(endpoint="regression", algorithm="ridge", split="scaffold",
                                   test_size=.25, cv_folds=5, y_scramble=1, seed=42)
    assert model.metrics["q2"] is None and model.q2 is None
    assert "5 folds need at least as many training groups" in model.metrics["cv_warning"]
    assert model.metrics["y_scramble"] is None
    assert "y_scramble_warning" in model.metrics
    assert "test" in model.metrics


def test_without_a_holdout_all_molecules_are_training_data(qsar_runtime):
    model = qsar_runtime.qsar.train(endpoint="regression", algorithm="ridge", split="scaffold",
                                   test_size=0, cv_folds=3, seed=42)
    assert (model.n_train, model.n_test) == (48, 0)
    assert model.metrics["q2"] is not None
    assert "test" not in model.metrics


def test_saved_q2_matches_independent_training_only_oof_predictions(qsar_runtime):
    from sklearn.base import clone
    from sklearn.metrics import r2_score
    from sklearn.model_selection import PredefinedSplit, cross_val_predict

    api = qsar_runtime.qsar
    model = api.train(endpoint="regression", algorithm="ridge", split="scaffold", test_size=.25,
                      cv_folds=3, y_scramble=1)
    train = {m.molecule_id: m for m in _members(qsar_runtime, model, "train")}
    records = [r for r in qsar_runtime.molecules.stream(qsar_runtime.molecules.select(role="ligand"))
               if r.id in train]
    with qsar_runtime.workspace.project_db.get_session() as session:
        _, fitted = api._load_fitted(session, model)
        ids, x, _names, _missing = api._design_matrix(session, records, feature_kind="rdkit2d",
                                                     features=fitted.feature_names, fp_radius=2, fp_nbits=2048)
    y = np.array([train[i].observed for i in ids])
    folds = np.array([train[i].fold for i in ids])
    predicted = cross_val_predict(clone(fitted.estimator), np.asarray(x, dtype=float), y, cv=PredefinedSplit(folds))
    assert model.metrics["q2"] == pytest.approx(r2_score(y, predicted))
    assert model.metrics["cv_rmse"] == pytest.approx(np.sqrt(np.mean((y - predicted) ** 2)))
    np.testing.assert_allclose([train[i].oof_predicted for i in ids], predicted)
    assert model.metrics["y_scramble_metric"] == "q2_pooled_oof"


def test_single_scaffold_holdout_fails_without_saving_a_model(qsar_runtime):
    from amdockvs.molecules.scopes import MoleculeScope

    api = qsar_runtime.qsar
    records = list(qsar_runtime.molecules.stream(qsar_runtime.molecules.select(role="ligand")))
    ids = [r.id for r in records if r.name.startswith("s0_")]
    scope = MoleculeScope(filters={"id__in": ids})
    before = len(api.list_models())
    with pytest.raises(ValueError, match="at least two groups"):
        api.train(endpoint="regression", algorithm="ridge", molecule_set=scope,
                  split="scaffold", test_size=.3)
    assert len(api.list_models()) == before
    with pytest.raises(ValueError, match="at least two groups"):
        api.split_preview(endpoint="regression", molecule_set=scope, split="scaffold", test_size=.3)
    # An explicitly requested no-holdout fit still works.
    model = api.train(endpoint="regression", algorithm="ridge", molecule_set=scope,
                      split="scaffold", test_size=0)
    assert model.n_train == 8
    assert "test" not in model.metrics


@pytest.mark.parametrize("split", ["stratified", "butina", "maxmin"])
def test_every_splitter_trains_and_previews(qsar_runtime, split):
    api = qsar_runtime.qsar
    preview = api.split_preview(endpoint="classification", split=split, test_size=.25, seed=1)
    model = api.train(endpoint="classification", task="classification", algorithm="logistic_regression",
                      split=split, test_size=.25, cv_folds=2, seed=1)
    assert (model.n_train, model.n_test) == (preview["n_train"], preview["n_test"])
    assert model.splitter == split


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


@pytest.mark.parametrize("source,expected", [("rdkit2d", "descriptors_2d"), ("ecfp4", "fingerprints_ecfp4")])
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
    assert (external.task, external.source) == ("regression", "external")
    assert runtime.qsar.predict(model=external)["predicted"] == 48
    with runtime.workspace.project_db.get_session() as session:
        code = session.connection().exec_driver_sql(
            "SELECT feature_type FROM qsar_models WHERE id = ?", (model.id,)).scalar()
        assert code == CODES["feature_type"][expected]
    assert CODES["feature_type"]["descriptors_2d"] == 1501  # existing codes must never move
