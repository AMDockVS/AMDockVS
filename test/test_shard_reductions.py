"""Diversity over a sharded library: the library stays in its shards and only the representatives
become molecule rows."""
import uuid

import pytest

pytest.importorskip("rdkit")
from sqlmodel import select

from amdockvs.io.shards import is_shard_key, shard_key, split_shard_key
from amdockvs.models import ImportBatch, MoleculeRecord
from amdockvs.runtime import AMDockVSRuntime

SERIES = ("{}c1ccccc1", "{}c1ccncc1", "{}C(=O)NC1CCCCC1")
LIBRARY = [(f"s{s}_{n}", tpl.format("C" * n)) for s, tpl in enumerate(SERIES) for n in range(1, 9)]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = AMDockVSRuntime()
    runtime.create_project(name="shard-reductions", folder=tmp_path / "project")
    library = tmp_path / "library.smi"
    library.write_text("".join(f"{smi} {name}\n" for name, smi in LIBRARY), encoding="utf-8")
    runtime.wait_for_jobs(runtime.loader.shard_ligands([library], shard_size=5), timeout_s=300)
    try:
        yield runtime
    finally:
        runtime.close_project()


def _completed(runtime, job_id) -> None:
    final = runtime.wait_for_jobs([job_id], timeout_s=300)
    assert all(str(r.status).lower().endswith("completed") for r in final.values()), final


def _ligands(runtime) -> dict[int, MoleculeRecord]:
    with runtime.workspace.project_db.get_session() as session:
        return {int(m.id): m for m in session.exec(select(MoleculeRecord))}


def test_shard_key_round_trips():
    key = shard_key(7, 12345)
    assert split_shard_key(key) == (7, 12345) and is_shard_key(key)
    assert not is_shard_key(12345)  # a molecule row id


def test_clustering_the_shards_promotes_only_the_representatives(runtime):
    assert not _ligands(runtime)  # the library is in its shards
    run_id = uuid.uuid4().hex
    _completed(runtime, runtime.diversity.cluster_job(
        method="bitbirch_lean", threshold=0.4, cluster_run_id=run_id, num_cpus=1, executor_name="thread", shards=True,
    ))
    run = runtime.diversity.get_run(run_id)
    promoted = _ligands(runtime)
    assert run and all(r["is_centroid"] for r in run)  # members stay by reference
    assert {r["molecule_id"] for r in run} == set(promoted)
    with runtime.workspace.project_db.get_session() as session:
        (batch,) = session.exec(select(ImportBatch)).all()
    names = dict(LIBRARY)
    for molecule in promoted.values():
        assert molecule.import_batch_id == batch.id and molecule.is_ligand
        assert molecule.name.split(":")[0] == "library"
    assert len(promoted) < len(names)

    # Picking again promotes what was not a row yet, and keeps the rows of earlier picks.
    picked = runtime.diversity.pick_representatives(run_id, mode="proportional", fraction=0.5)
    again = runtime.diversity.get_run(run_id)
    assert sum(r["is_centroid"] for r in again) == picked["n_reps"] > len(run)
    assert set(promoted) <= {r["molecule_id"] for r in again} == set(_ligands(runtime))
    assert runtime.diversity.pick_representatives(run_id, mode="proportional", fraction=0.5) == picked
    assert len(_ligands(runtime)) == len(again)  # the same picks again: no duplicate rows


@pytest.mark.parametrize("feature_source", ["descriptors", "ecfp4"])
def test_qsar_scores_the_shards_by_reference_and_promotes_the_cut(runtime, tmp_path, feature_source):
    pytest.importorskip("sklearn")
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import Crippen

    # The same molecules as rows (another order: another file), measured, to train on.
    training = tmp_path / "training.smi"
    training.write_text("".join(f"{smi} t_{name}\n" for name, smi in reversed(LIBRARY)), encoding="utf-8")
    runtime.wait_for_jobs(runtime.loader.load_ligands([training], executor_name="thread"), timeout_s=300)
    table = tmp_path / "activities.csv"
    table.write_text("smiles,value\n" + "".join(
        f"{smi},{4 + Crippen.MolLogP(Chem.MolFromSmiles(smi)):.3f}\n" for _name, smi in LIBRARY), encoding="utf-8")
    runtime.qsar.load_activities(table, endpoint="pAct", match_by="smiles")
    _completed(runtime, runtime.qsar.compute_descriptors(only_missing=False, compute_fingerprints=True,
                                                         executor_name="thread"))
    model = runtime.qsar.train(endpoint="pAct", algorithm="ridge", feature_source=feature_source)
    runtime.qsar.predict(model=model)
    rows = sorted(r["value"] for r in runtime.qsar.prediction_rows(model=model))
    before = set(_ligands(runtime))

    _completed(runtime, runtime.qsar.predict_shards(model=model, executor_name="thread"))
    frame = runtime.qsar.shard_predictions(model=model)
    assert len(frame) == len(LIBRARY) and all(is_shard_key(k) for k in frame["key"])
    assert np.allclose(sorted(frame["value"]), rows)  # the features a shard computes are the rows'
    assert set(_ligands(runtime)) == before  # scored by reference: no row yet

    cut = runtime.qsar.promote_shard_predictions(model=model, top_n=3)
    assert (cut["scored"], cut["kept"]) == (len(LIBRARY), 3)
    promoted = set(_ligands(runtime)) - before
    assert promoted == set(cut["molecule_ids"])
    values = {p.molecule_id: p.value for p in runtime.qsar.list_predictions(model=model, ligand_ids=list(promoted))}
    assert sorted(values.values(), reverse=True) == sorted(frame["value"], reverse=True)[:3]
    assert runtime.qsar.promote_shard_predictions(model=model, top_n=3)["molecule_ids"] == cut["molecule_ids"]
    assert set(_ligands(runtime)) - before == promoted  # cutting again reuses the rows
