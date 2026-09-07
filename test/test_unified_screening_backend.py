from types import SimpleNamespace

import pytest
from sqlmodel import select

from amdockvs.chemistry.api import ChemistryAPI
from amdockvs.core.configuration import app_config
from amdockvs.docking.api import DockingAPI
from amdockvs.screening.campaign import advance_shard_run, completed_shard_targets, live_target_progress
from amdockvs.models import ScreeningShard, ScreeningShardRun, ShardEngineState
from amdockvs.molecules.storage import iter_prepared_shards
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.core.vocab import TargetState


def _project_with_shard(tmp_path):
    runtime = AMDockVSRuntime()
    runtime.create_project(name="unified-backend", folder=tmp_path)
    source = tmp_path / "raw.mshard"
    source.write_bytes(b"raw")
    with runtime.molsuite.project_db.get_session() as session:
        shard = ScreeningShard(
            source="library.smi",
            shard_index=0,
            path=str(source),
            input_format="smiles",
            n_records=100,
            state="ready",
        )
        session.add(shard)
        session.commit()
        session.refresh(shard)
    return runtime, shard, source


def test_engine_artifact_does_not_replace_the_chemical_shard(tmp_path):
    runtime, shard, source = _project_with_shard(tmp_path)
    prepared = tmp_path / "prepared_ad4" / "raw.mshard"
    prepared.parent.mkdir()
    prepared.write_bytes(b"pdbqt")
    try:
        assert app_config(runtime).shards.keep_history is False
        with runtime.molsuite.project_db.get_session() as session:
            session.add(
                ShardEngineState(
                    shard_id=int(shard.id),
                    engine="ad4",
                    files={"source": str(source), "prepared": str(prepared)},
                    n_records=97,
                    is_ready=True,
                )
            )
            session.commit()

        rows = list(iter_prepared_shards(runtime.molsuite.project_db, engine="ad4"))
        assert len(rows) == 1
        assert rows[0]["id"] == int(shard.id)
        assert rows[0]["path"] == str(source)
        assert rows[0]["prepared_path"] == str(prepared)
        assert rows[0]["n_records"] == 97
        assert runtime.molecules.shard_counts(engine="ad4") == {
            "shards": 1,
            "records": 100,
            "prepared": 1,
            "prepared_records": 97,
        }
        with runtime.molsuite.project_db.get_session() as session:
            inventory = session.get(ScreeningShard, int(shard.id))
            assert inventory.path == str(source)
            assert inventory.input_format == "smiles"
    finally:
        runtime.close_project()


def test_physical_shard_size_is_separate_from_ligand_job_batch():
    from amdockvs.io.api import default_shard_size

    config = app_config().model_copy(deep=True)
    config.batch_sizes.ligand = 5
    config.shards.records_per_shard = 17
    runtime = SimpleNamespace(
        amdock_configuration=SimpleNamespace(get_value=lambda _path: config),
    )

    assert default_shard_size(runtime=runtime) == 17


def test_shard_completion_is_scoped_to_run_engine_and_protocol(tmp_path):
    runtime, shard, _source = _project_with_shard(tmp_path)
    db = runtime.molsuite.project_db
    try:
        advance_shard_run(
            db,
            run_id="run-a",
            shard_id=int(shard.id),
            receptor_id=10,
            engine="vina",
            protocol_hash="p1",
            scored=100,
            hits=3,
            result_path="results/docking/screening/run-a/results.mshard",
        )
        assert completed_shard_targets(db, run_id="run-a", engine="vina", protocol_hash="p1") == {
            (int(shard.id), 10)
        }

        advance_shard_run(
            db,
            run_id="run-a",
            shard_id=int(shard.id),
            receptor_id=10,
            engine="vina",
            protocol_hash="p1",
            scored=100,
            hits=1,
        )
        assert completed_shard_targets(db, run_id="run-b", engine="vina", protocol_hash="p1") == set()
        assert completed_shard_targets(db, run_id="run-a", engine="vina", protocol_hash="p2") == set()

        with db.get_session() as session:
            receipt = session.exec(select(ScreeningShardRun)).one()
            assert receipt.state == TargetState.DONE
            assert receipt.result_path.endswith("results.mshard")
            # Replaying the same pair is idempotent.
            assert (receipt.ligands_scored, receipt.hits) == (100, 3)
    finally:
        runtime.close_project()


def test_live_target_progress_adds_only_matching_inflight_chunk_work():
    targets = [
        {"id": 1, "run_id": "old", "receptor_molecule_id": 7, "ligands_total": 50, "ligands_done": 50},
        {"id": 2, "run_id": "run", "receptor_molecule_id": 7, "ligands_total": 100, "ligands_done": 20},
        {"id": 3, "run_id": "run", "receptor_molecule_id": 8, "ligands_total": 100, "ligands_done": 0},
    ]
    chunks = [
        {"progress": 50.0, "payload_json": '{"run_id":"run","receptor_id":7,"shards":[{"n_records":40}]}'},
        {"progress": 25.0, "payload_json": '{"run_id":"run","receptor_id":8,"shards":[{"n_records":100}]}'},
        {"progress": 100.0, "payload_json": '{"run_id":"old","receptor_id":7,"shards":[{"n_records":50}]}'},
    ]

    assert live_target_progress(targets, chunks) == {
        7: {"ligands": 100, "docked": 40, "pending": 60},
        8: {"ligands": 100, "docked": 25, "pending": 75},
    }


def test_ligand_pipeline_selects_the_shard_adapter_without_a_row_scope(monkeypatch):
    runtime = SimpleNamespace(
        molsuite=SimpleNamespace(project_db=object()),
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
        molsuite=SimpleNamespace(project_db=object()),
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
