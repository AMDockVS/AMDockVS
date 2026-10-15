"""QSAR end to end, the way a screening uses it:

a. load activities                 d. score every ligand with the best model
b. pick a diverse, activity-spanning training set (Diversity)
c. train and evaluate several models on it (scaffold split + CV)
e. from the Predictions view, keep a top-N / a cutoff and exclude the rest
"""
import sys
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("rdkit")
pytest.importorskip("sklearn")
from PySide6.QtWidgets import QApplication, QMessageBox
from rdkit import Chem
from rdkit.Chem import Crippen
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.models.molecules import MoleculeRecord
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import PREDICTIONS_VIEW_ID

ENDPOINT = "pAct"
# Four chemical series; the chain length drives the (synthetic) activity within each.
SERIES = ("{}c1ccccc1", "{}c1ccncc1", "{}C(=O)NC1CCCCC1", "{}Oc1ccc2[nH]ccc2c1")
LABELED = [(f"s{s}_{n}", tpl.format("C" * n)) for s, tpl in enumerate(SERIES) for n in range(1, 11)]
UNLABELED = [(f"u{n}", f"{'C' * n}c1ccc(O)cc1") for n in range(1, 13)]


def _activity(smiles: str) -> float:
    return round(4.0 + Crippen.MolLogP(Chem.MolFromSmiles(smiles)), 3)


def _wait(app, done, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert done()


@pytest.mark.filterwarnings("ignore")
def test_qsar_from_activities_to_a_filtered_library(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    monkeypatch.setattr("amdockvs.ui.tools.qsar.panels.QMessageBox.question", lambda *a, **k: QMessageBox.Yes)
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window = None
    runtime = AMDockVSRuntime()

    def completed(job_ids, timeout_s=300) -> None:
        final = runtime.wait_for_jobs([job_ids] if isinstance(job_ids, str) else list(job_ids), timeout_s=timeout_s)
        assert all(str(r.status).lower().endswith("completed") for r in final.values()), final

    def active_ligands() -> set[int]:
        with runtime.workspace.project_db.get_session() as session:
            return set(session.exec(select(MoleculeRecord.id).where(MoleculeRecord.excluded == False)).all())  # noqa: E712

    try:
        runtime.create_project(name="qsar", folder=tmp_path / "qsar", description="qsar e2e")
        library = tmp_path / "library.smi"
        library.write_text("".join(f"{smi} {name}\n" for name, smi in LABELED + UNLABELED), encoding="utf-8")
        completed(runtime.loader.load_ligands([library], batch_size=32, executor_name="thread"))
        with runtime.workspace.project_db.get_session() as session:
            by_name = {str(m.name): int(m.id) for m in session.exec(select(MoleculeRecord))}
        assert len(by_name) == len(LABELED) + len(UNLABELED)
        labeled_ids = {by_name[name] for name, _ in LABELED}

        # a. activities, matched to the ligands by structure
        table = tmp_path / "activities.csv"
        table.write_text("smiles,value\n" + "".join(f"{smi},{_activity(smi)}\n" for _, smi in LABELED), encoding="utf-8")
        loaded = runtime.qsar.load_activities(table, endpoint=ENDPOINT, match_by="smiles")
        measured = {int(a.molecule_id): float(a.value) for a in runtime.qsar.list_activities(endpoint=ENDPOINT)}
        assert set(measured) == labeled_ids, loaded
        completed(runtime.qsar.compute_descriptors(only_missing=False, compute_fingerprints=True, executor_name="thread"))

        # b. a training set that is diverse AND spans the activity. Cluster the measured ligands
        #    ONCE; what represents each cluster is then chosen, and chosen again, on that run.
        run_id = uuid.uuid4().hex
        completed(runtime.diversity.cluster_job(
            method="bitbirch_lean", threshold=0.4, with_activity=ENDPOINT,
            cluster_run_id=run_id, num_cpus=1, executor_name="thread",
        ))
        run = runtime.diversity.get_run(run_id)
        assert {r["molecule_id"] for r in run} == labeled_ids  # only ligands with a value were clustered
        clusters: dict[int, dict[int, float]] = {}
        for row in run:
            clusters.setdefault(row["cluster_id"], {})[row["molecule_id"]] = measured[row["molecule_id"]]
        assert len(clusters) >= len(SERIES)  # the series do not merge
        assert sum(r["is_centroid"] for r in run) == len(clusters)  # as clustered: one each

        def representatives() -> set[int]:
            again = runtime.diversity.get_run(run_id)
            assert {r["molecule_id"]: r["cluster_id"] for r in again} == {r["molecule_id"]: r["cluster_id"] for r in run}
            return {r["molecule_id"] for r in again if r["is_centroid"]}

        # half of every cluster: each weighs in the result what it weighs in the run
        picked = runtime.diversity.pick_representatives(run_id, mode="proportional", fraction=0.5, rank_by=ENDPOINT)
        half = representatives()
        assert picked["n_reps"] == len(half) and picked["n_clusters"] == len(clusters)
        for members in clusters.values():
            assert len(half & set(members)) == max(1, round(0.5 * len(members)))
        # one per activity quartile: from each cluster, its lowest to its highest
        runtime.diversity.pick_representatives(run_id, mode="quartiles", rank_by=ENDPOINT)
        picked = representatives()
        for members in clusters.values():
            kept = [value for mid, value in members.items() if mid in picked]
            assert len(kept) == min(4, len(members))
            assert min(kept) == min(members.values()) and max(kept) == max(members.values())
        assert len(picked) < len(labeled_ids)  # and it is a reduction
        assert {min(measured.values()), max(measured.values())} <= {measured[i] for i in picked}
        training = runtime.diversity.save_centroids_as_set(run_id, name="training")

        # c. several models on that set: scaffold split + CV, then scored on EVERY measured ligand
        #    (most of which the models never saw)
        scores = {}
        for algorithm in ("ridge", "random_forest"):
            model = runtime.qsar.train(
                endpoint=ENDPOINT, molecule_set=training, algorithm=algorithm, task="regression",
                split="scaffold", test_size=0.3, cv_folds=3, name=algorithm,
            )
            assert {"train", "test", "q2"} <= set(model.metrics), sorted(model.metrics)
            assert 0 < model.metrics["n_train"] < len(picked)
            scores[int(model.id)] = runtime.qsar.evaluate(model=model, endpoint=ENDPOINT)["r2"]
        best = max(scores, key=scores.get)
        assert scores[best] > 0.7, scores

        # d. the best model over the whole library
        result = runtime.qsar.predict(model=best)
        assert result["predicted"] == len(by_name), result
        predicted = {row["molecule_id"]: row["value"] for row in runtime.qsar.prediction_rows(model=best)}
        best_first = sorted(predicted, key=lambda i: (-predicted[i], i))
        cut = runtime.qsar.cut_predictions(model=best, top_n=5, ids_limit=3)
        assert (cut["kept"], cut["rest"], cut["excluded"], cut["ids"]) == (5, len(by_name) - 5, 0, best_first[:3])
        lowest = runtime.qsar.cut_predictions(model=best, top_n=2, higher_is_better=False, ids_limit=9)
        assert lowest["ids"] == sorted(predicted, key=lambda i: (predicted[i], i))[:2]

        # e. the Predictions view as a filter
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        view = window.open_or_focus_view(PREDICTIONS_VIEW_ID)
        _wait(app, lambda: view.model_combo.findData(best) >= 0)
        view.model_combo.setCurrentIndex(view.model_combo.findData(best))
        _wait(app, lambda: view.table.rowCount() == len(by_name))

        # a cutoff, tried first as a selection: nothing is excluded
        cutoff = sorted(predicted.values())[len(predicted) // 2]
        view.cut_mode.setCurrentIndex(view.cut_mode.findText("Predicted ≥"))
        view.cut_value.setValue(cutoff)
        cutoff = view.cut_value.value()  # as the spin box rounds it
        view.select_button.click()
        _wait(app, lambda: bool(window.selection.get("ligand")))
        assert set(window.selection.get("ligand")) == {i for i, v in predicted.items() if v >= cutoff}
        assert active_ligands() == set(by_name.values())
        window.selection.set("ligand", None)

        # a top-N: every other ligand is excluded
        top = best_first[:10]
        view.cut_mode.setCurrentIndex(view.cut_mode.findText("Top N highest"))
        view.cut_value.setValue(10)
        view.exclude_button.click()
        _wait(app, lambda: active_ligands() == set(top))
        _wait(app, lambda: view.table.rowCount() == 10 and f"excluded {len(by_name) - 10}" in view.status.text())
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
