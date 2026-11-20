from types import SimpleNamespace

import pytest
from sqlmodel import select

from amdockvs.chemistry.api import ChemistryAPI
from amdockvs.core.configuration import app_config
from amdockvs.docking.api import DockingAPI
from amdockvs.screening.campaign import advance_shard_run, completed_shard_targets
from amdockvs.models import ImportBatch, ScreeningShardRun, ShardPrep
from ms_flow.core.data import catalog
from catalog_seed import seed_library
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
    return runtime, shard_id, tmp_path / "raw.parquet"


def test_engine_artifact_does_not_replace_the_chemical_shard(tmp_path):
    runtime, shard_id, source = _project_with_shard(tmp_path)
    prepared = tmp_path / "prepared_ad4" / "raw.0.parquet"
    prepared.parent.mkdir()
    prepared.write_bytes(b"pdbqt")
    try:
        assert app_config(runtime).shards.keep_history is False
        with runtime.workspace.project_db.get_session() as session:
            session.add(
                ShardPrep(shard_id=shard_id, family="ad4", n_records=97)
            )
            session.commit()

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


def test_shard_completion_is_scoped_to_run_engine_and_protocol(tmp_path):
    runtime, shard_id, _source = _project_with_shard(tmp_path)
    db = runtime.workspace.project_db
    try:
        from amdockvs.docking.results.repository import docking_run_id

        def run(run_id, protocol_hash):
            return docking_run_id(db, run_id=run_id, engine="vina", run_kind="screening",
                                  protocol={"hash": protocol_hash})

        advance_shard_run(
            db,
            docking_run_id=run("run-a", "p1"),
            shard_id=shard_id,
            receptor_id=10,
            engine="vina",
            protocol_hash="p1",
            scored=100,
            hits=3,
            result_path="results/docking/screening/run-a/results.parquet",
        )
        assert completed_shard_targets(db, docking_run_id=run("run-a", "p1")) == {
            (shard_id, 10, 0)  # no binding site: the whole receptor is the target
        }

        advance_shard_run(
            db,
            docking_run_id=run("run-a", "p1"),
            shard_id=shard_id,
            receptor_id=10,
            engine="vina",
            protocol_hash="p1",
            scored=100,
            hits=1,
        )
        assert completed_shard_targets(db, docking_run_id=run("run-b", "p1")) == set()
        assert completed_shard_targets(db, docking_run_id=run("run-a", "p2")) == set()

        with db.get_session() as session:
            receipt = session.exec(select(ScreeningShardRun)).one()
            assert receipt.state == TargetState.DONE
            assert receipt.result_path.endswith("results.parquet")
            # Replaying the same pair is idempotent.
            assert (receipt.ligands_scored, receipt.hits) == (100, 3)
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


def test_shard_prep_rows_upsert_through_the_job_sink(tmp_path):
    from amdockvs.core.rows import shard_prep_row
    from amdockvs.docking.preparation.jobs import PrepareLigandShardsJobSpec

    runtime, shard_id, _source = _project_with_shard(tmp_path)
    try:
        project_db = runtime.workspace.project_db
        sink = PrepareLigandShardsJobSpec.output_spec
        project_db.persist_output_spec(sink, [shard_prep_row(shard_id=shard_id, family="ad4", n_records=90)])
        project_db.persist_output_spec(sink, [shard_prep_row(shard_id=shard_id, family="ad4", n_records=97, n_failed=3)])
        with project_db.get_session() as session:
            stored = session.exec(select(ShardPrep)).all()
        assert [(row.shard_id, row.family, row.n_records, row.n_failed) for row in stored] == [
            (shard_id, "ad4", 97, 3)
        ]
    finally:
        runtime.close_project()
