"""The docking campaign: what its stages do there, and that what comes back here is written once."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.shard_tasks import ResultFragmentWriter, by_fragment, hit_poses, off_target_elements, rank


def _fragment(path: Path, scores: dict[tuple[int, int], float]) -> Path:
    with ResultFragmentWriter(path, metadata={}) as fragment:
        for (ligand, receptor), score in scores.items():
            fragment.add({"ligand_id": ligand, "receptor_id": receptor, "score": score, "score_type": "vina_score",
                          "status": "done", "engine": "vina", "smiles": "CCO", "metrics_json": "{}",
                          "pose_text": f"pose-{ligand}-{receptor}", "pose_suffix": ".pdbqt"})
    return path


def _result(path: Path, shard_id: int) -> dict:
    return {"stage": "vs", "envelopes": [{"shard_id": shard_id, "result_shard": str(path), "library": "lib",
                                          "import_batch_id": 3}]}


def test_the_far_stages_rank_read_poses_and_send_the_reference_best_to_the_off_targets(tmp_path):
    a = _fragment(tmp_path / "a.parquet", {(1, 10): -9.0, (2, 10): -7.0, (3, 10): -11.0, (1, 20): -10.0})
    b = _fragment(tmp_path / "b.parquet", {(4, 10): -10.0, (5, 10): -5.0})

    refs = rank([_result(a, 1), _result(b, 2)], cap=2, threshold=-6.0)
    assert [(ref["ligand_id"], ref["receptor_id"]) for ref in refs] == [(3, 10), (4, 10), (1, 20)]

    elements = by_fragment(refs)
    assert sorted(len(element["refs"]) for element in elements) == [1, 2]
    hits = [hit for element in elements for hit in hit_poses(element)["hits"]]
    assert {(hit["name"], hit["_pose_text"], hit["import_batch_id"]) for hit in hits} == {
        ("lib:3", "pose-3-10", 3), ("lib:4", "pose-4-10", 3), ("lib:1", "pose-1-20", 3)}

    shards = {1: {"shard_id": 1, "path": "s1"}, 2: {"shard_id": 2, "path": "s2"}}
    targets = [{"receptor_id": 30}, {"receptor_id": 40}]
    off = list(off_target_elements(refs, {"engine": "vina"}, targets, shards, 10, 1))
    assert [(e["receptor_id"], e["shards"][0]["shard_id"], e["shards"][0]["record_ids"]) for e in off] == [
        (30, 1, [3]), (40, 1, [3])]  # only the reference's best one, against each off-target
    assert all(e["stage"] == "off" and e["selected_from_receptor_id"] == 10 for e in off)


def test_what_comes_back_twice_is_written_once(tmp_path):
    from sqlmodel import select

    from amdockvs.docking.campaign import CampaignWriter
    from amdockvs.docking.results.repository import docking_run_id
    from amdockvs.models import (
        DockingResultRecord, ImportBatch, MoleculeRecord, ScreeningShard, ScreeningShardRun, ScreeningTarget,
    )
    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.screening.campaign import plan_targets

    runtime = AMDockVSRuntime()
    runtime.create_project(name="campaign-writer", folder=tmp_path)
    try:
        db = runtime.workspace.project_db
        with db.get_session() as session:
            batch = ImportBatch(source="lib.smi", quick_hash="lib", kind="shards")
            receptor = MoleculeRecord(name="R", molecule_type="protein", is_receptor=True)
            session.add_all([batch, receptor])
            session.flush()
            shard = ScreeningShard(import_batch_id=int(batch.id), shard_index=0, path="lib.parquet", n_records=2)
            session.add(shard)
            session.commit()
            receptor_id, shard_id, batch_id = int(receptor.id), int(shard.id), int(batch.id)
        runs = {run_id: docking_run_id(db, run_id=run_id, engine="vina", run_kind="screening", protocol={"hash": "p"})
                for run_id in ("r1", "r2")}
        for run in runs.values():
            plan_targets(db, docking_run_id=run, receptors=[{"receptor_id": receptor_id}], ligands_total=2, engine="vina")
        fragment = _fragment(tmp_path / "far.parquet", {(1, receptor_id): -9.0, (2, receptor_id): -8.0})
        [element] = by_fragment(rank([{"stage": "vs", "envelopes": [
            {"shard_id": shard_id, "result_shard": str(fragment), "library": "lib", "import_batch_id": batch_id}]}],
            cap=2, threshold=-6.0))
        results = [{"stage": "vs", "envelopes": [{"shard_id": shard_id, "receptor_id": receptor_id, "scored": 2,
                                                  "hits": [], "hits_found": 2, "result_shard": str(fragment)}]},
                   hit_poses(element)]

        for run_id in ("r1", "r1", "r2"):  # a campaign submitted again sends it all again, maybe as another run
            writer = CampaignWriter(project_db=db, hit_cap=2, hit_threshold=-6.0, run_id=run_id, engine="vina",
                                    protocol_hash="p")
            writer.write(results)
            writer.finish()

        with db.get_session() as session:
            results = session.exec(select(DockingResultRecord)).all()
            receipts = session.exec(select(ScreeningShardRun)).all()
            imported = {t.docking_run_id: t.imported for t in session.exec(select(ScreeningTarget)).all()}
        assert sorted(row.docking_run_id for row in results) == [runs["r2"]] * 2  # moved, not copied
        assert len(receipts) == 2 and {r.result_path for r in receipts} == {""}  # one per run; the fragment stayed there
        assert imported[runs["r2"]] == 2
    finally:
        runtime.close_project()


def test_an_old_project_gets_the_columns_added_since(tmp_path):
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="digests", folder=tmp_path)
    added = {"shard_preps": "sha256", "screening_targets": "forced"}
    try:
        with runtime.workspace.project_db.get_session() as session:
            conn = session.connection()
            for table, column in added.items():
                conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
            session.commit()
        runtime._add_columns()
        with runtime.workspace.project_db.get_session() as session:
            for table, column in added.items():
                columns = session.connection().exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                assert column in {str(row[1]) for row in columns}
    finally:
        runtime.close_project()
