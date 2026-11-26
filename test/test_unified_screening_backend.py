from types import SimpleNamespace

import pytest
from sqlmodel import select

from amdockvs.chemistry.api import ChemistryAPI
from amdockvs.core.configuration import app_config
from amdockvs.docking.api import DockingAPI
from amdockvs.docking.results.dataset import state_key
from amdockvs.screening.campaign import done_shards, record_docked
from amdockvs.models import ImportBatch
from ms_flow.core.data import catalog
from catalog_seed import seed_library, seed_prepared
from amdockvs.molecules.storage import iter_prepared_shards
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.core.vocab import TargetState


def _project_with_shard(tmp_path):
    runtime = AMDockVSRuntime()
    runtime.create_project(name="unified-backend", folder=tmp_path)
    with runtime.workspace.project_db.get_session() as session:
        batch = ImportBatch(source="library.smi", quick_hash="x", format="smiles", kind="shards")
        session.add(batch)
        session.commit()
        session.refresh(batch)
    _file_id, (shard_id,) = seed_library(runtime.workspace.project_db, tmp_path, scope=int(batch.id),
                                         records=100, name="raw")
    return runtime, shard_id, tmp_path / "raw.arrows"


def test_engine_artifact_does_not_replace_the_chemical_shard(tmp_path):
    runtime, shard_id, source = _project_with_shard(tmp_path)
    try:
        assert app_config(runtime).shards.keep_history is False
        prepared = seed_prepared(runtime.workspace.project_db, catalog.get_shard(runtime.workspace.project_db, shard_id),
                                 tmp_path, dropped={0, 1, 2})

        rows = list(iter_prepared_shards(runtime.workspace.project_db, engine="ad4"))
        assert len(rows) == 1
        assert rows[0]["id"] == shard_id
        assert rows[0]["path"] == str(source)
        assert rows[0]["prepared_path"] == str(prepared)
        assert rows[0]["n_records"] == 97
        assert runtime.molecules.shard_counts(engine="ad4") == {
            "shards": 1,
            "records": 100,
            "prepared": 1,
            "prepared_records": 97,
        }
        inventory = catalog.get_shard(runtime.workspace.project_db, shard_id)
        assert inventory["path"] == str(source)
        with runtime.workspace.project_db.get_session() as session:
            assert session.get(ImportBatch, inventory["scope"]).format == "smiles"
    finally:
        runtime.close_project()


def test_shard_completion_is_scoped_to_engine_protocol_and_receptor(tmp_path):
    runtime, shard_id, _source = _project_with_shard(tmp_path)
    db = runtime.workspace.project_db
    try:
        def key(protocol_hash, receptor_id=10, engine="vina"):
            return state_key(engine=engine, protocol_hash=protocol_hash, receptor_id=receptor_id)

        assert record_docked(db, key=key("p1"), shard_id=shard_id) is True
        assert done_shards(db, key("p1")) == {shard_id}
        # Another protocol, another receptor or another engine is another key: nothing is done.
        assert done_shards(db, key("p2")) == set()
        assert done_shards(db, key("p1", receptor_id=11)) == set()
        assert done_shards(db, key("p1", engine="qvina")) == set()
        # Replaying the same pair writes nothing and says so.
        assert record_docked(db, key=key("p1"), shard_id=shard_id) is False
    finally:
        runtime.close_project()


def test_a_docked_shard_records_its_ligand_counts(tmp_path):
    from ms_flow.core.data import states

    from amdockvs.screening.campaign import DONE_KIND

    runtime, shard_id, _source = _project_with_shard(tmp_path)
    db = runtime.workspace.project_db
    try:
        key = state_key(engine="vina", protocol_hash="p1", receptor_id=10)
        assert record_docked(db, key=key, shard_id=shard_id, done_ids=[0, 1, 3], failed_ids=[2]) is True
        (row,) = states.progress(db, DONE_KIND)
        assert (row["shards_done"], row["records_done"], row["records_failed"]) == (1, 3, 1)
        assert states.failed(db, DONE_KIND, key, shard_id).tolist() == [2]
    finally:
        runtime.close_project()


def test_ligand_pipeline_selects_the_shard_adapter_without_a_row_scope(monkeypatch):
    runtime = SimpleNamespace(
        workspace=SimpleNamespace(project_db=object()),
        _require_active_project=lambda: None,
    )
    api = ChemistryAPI(runtime)
    seen = {}
    monkeypatch.setattr("amdockvs.chemistry.api.has_shards", lambda _db: True)
    monkeypatch.setattr(
        api,
        "run_shard_pipeline",
        lambda steps, **kwargs: seen.update(steps=steps, **kwargs) or "shard-job",
    )

    assert api.run_ligand_pipeline(["standardize", ("generate_3d", {"seed": 7})]) == "shard-job"
    assert seen["steps"] == [("standardize", {}), ("generate_3d", {"seed": 7})]


def test_shard_pipeline_rejects_an_ensemble_it_cannot_store():
    api = ChemistryAPI(SimpleNamespace(_require_active_project=lambda: None))
    with pytest.raises(ValueError, match="conformer ensembles"):
        api.run_shard_pipeline(["generate_3d", "conformers"])


def test_docking_public_methods_select_shards_without_a_ligand_scope(monkeypatch):
    runtime = SimpleNamespace(
        workspace=SimpleNamespace(project_db=object()),
        _require_active_project=lambda: None,
    )
    api = DockingAPI(runtime)
    monkeypatch.setattr("amdockvs.docking.api.has_shards", lambda _db: True)
    prepared = {}
    docked = {}
    monkeypatch.setattr(
        api,
        "prepare_ligand_shards",
        lambda **kwargs: prepared.update(kwargs) or "prepare-shards",
    )
    monkeypatch.setattr(api, "run_shards", lambda **kwargs: docked.update(kwargs) or "dock-shards")

    assert api.prepare_ligands(program="vina", batch_size=2) == "prepare-shards"
    assert prepared["program"] == "vina"
    assert "batch_size" not in prepared
    assert api.run(
        program="vina",
        receptor_set=7,
        hit_threshold=-8.0,
        hit_cap=500,
        batch_size=3,
    ) == "dock-shards"
    assert docked["receptor_set"] == 7
    assert (docked["hit_threshold"], docked["hit_cap"]) == (-8.0, 500)
    assert "batch_size" not in docked
