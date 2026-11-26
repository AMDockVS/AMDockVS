"""The docking campaign: what its stages do there, and that what comes back here is written once."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.shard_tasks import ResultFragmentWriter, by_fragment, hit_poses, off_target_elements, rank


def _fragment(path: Path, scores: dict[tuple[int, int], float], *, shard_id: int = 0) -> Path:
    with ResultFragmentWriter(path, metadata={}) as fragment:
        for (ligand, receptor), score in scores.items():
            fragment.add({"ligand_id": ligand, "shard_id": shard_id,
                          "receptor_id": receptor, "score": score, "score_type": "vina_score",
                          "status": "done", "engine": "vina", "smiles": "CCO", "metrics_json": "{}",
                          "pose_text": f"pose-{ligand}-{receptor}", "pose_suffix": ".pdbqt"})
    return path


def _unpacked(payload) -> str:
    import zlib

    if payload.codec == "zlib":
        return zlib.decompress(payload.data).decode()
    import zstandard

    return zstandard.ZstdDecompressor().decompress(payload.data, max_output_size=payload.raw_length).decode()


def _result(path: Path, shard_id: int) -> dict:
    return {"stage": "vs", "envelopes": [{"shard_id": shard_id, "result_shard": str(path), "library": "lib",
                                          "import_batch_id": 3}]}


def test_the_far_stages_rank_read_poses_and_send_the_reference_best_to_the_off_targets(tmp_path):
    a = _fragment(tmp_path / "a.arrows", {(1, 10): -9.0, (2, 10): -7.0, (3, 10): -11.0, (1, 20): -10.0}, shard_id=1)
    b = _fragment(tmp_path / "b.arrows", {(4, 10): -10.0, (5, 10): -5.0}, shard_id=2)

    refs = rank([_result(a, 1), _result(b, 2)], cap=2, threshold=-6.0)
    assert [(ref["ligand_id"], ref["receptor_id"]) for ref in refs] == [(3, 10), (4, 10), (1, 20)]

    elements = by_fragment(refs)
    assert sorted(len(element["refs"]) for element in elements) == [1, 2]
    hits = [hit for element in elements for hit in hit_poses(element)["hits"]]
    assert all(hit["pose"].raw_length is not None for hit in hits)  # compressed there, not here
    assert {(hit["name"], _unpacked(hit["pose"]), hit["import_batch_id"]) for hit in hits} == {("lib:3", "pose-3-10", 3), ("lib:4", "pose-4-10", 3), ("lib:1", "pose-1-20", 3)}

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
    from amdockvs.docking.results.dataset import state_key
    from amdockvs.models import DockingResultRecord, ImportBatch, MoleculeRecord, ScreeningTarget
    from amdockvs.screening.campaign import done_shards
    from amdockvs.runtime import AMDockVSRuntime
    from catalog_seed import seed_library
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
            session.commit()
            receptor_id, batch_id = int(receptor.id), int(batch.id)
        _file_id, (shard_id,) = seed_library(db, tmp_path, scope=batch_id, records=2)
        runs = {run_id: docking_run_id(db, run_id=run_id, engine="vina", run_kind="screening", protocol={"hash": "p"})
                for run_id in ("r1", "r2")}
        key = state_key(engine="vina", protocol_hash="p", receptor_id=receptor_id)
        for run in runs.values():
            plan_targets(db, docking_run_id=run, receptors=[{"receptor_id": receptor_id, "state_key": key}],
                         ligands_total=2, engine="vina")
        fragment = _fragment(tmp_path / "far.arrows", {(1, receptor_id): -9.0, (2, receptor_id): -8.0},
                             shard_id=shard_id)
        [element] = by_fragment(rank([{"stage": "vs", "envelopes": [
            {"shard_id": shard_id, "result_shard": str(fragment), "library": "lib", "import_batch_id": batch_id}]}],
            cap=2, threshold=-6.0))
        results = [{"stage": "vs", "envelopes": [{"shard_id": shard_id, "receptor_id": receptor_id, "scored": 2,
                                                  "hits": [], "hits_found": 2, "result_shard": str(fragment),
                                                  "state_key": key}]},
                   hit_poses(element)]

        for run_id in ("r1", "r1", "r2"):  # a campaign submitted again sends it all again, maybe as another run
            writer = CampaignWriter(project_db=db, hit_cap=2, hit_threshold=-6.0, run_id=run_id, engine="vina",
                                    protocol_hash="p")
            writer.write(results)
            writer.finish()

        with db.get_session() as session:
            results = session.exec(select(DockingResultRecord)).all()
            imported = {t.docking_run_id: t.imported for t in session.exec(select(ScreeningTarget)).all()}
        assert sorted(row.docking_run_id for row in results) == [runs["r2"]] * 2  # moved, not copied
        # The shard is done once, whichever run sent it back: the fragment stayed there, so
        # nothing of it is in the catalog here.
        assert done_shards(db, key) == {shard_id}
        assert imported[runs["r2"]] == 2
    finally:
        runtime.close_project()


def test_an_old_project_gets_the_columns_added_since(tmp_path):
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="digests", folder=tmp_path)
    added = {"receptor_preps": "warnings"}
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


def test_a_fragment_is_two_one_shard_parts_and_only_the_payload_holds_poses(tmp_path):
    from ms_flow.core.data.shard import file_groups, read_rows

    from amdockvs.docking.results.dataset import payload_part

    summary = _fragment(tmp_path / "f.arrows", {(i, 10): -float(i) for i in range(1, 200)})
    for part in (summary, payload_part(summary)):
        assert [group.n_records for group in file_groups(part)] == [199]
    assert "pose_text" not in read_rows(summary).column_names
    assert "pose_text" in read_rows(payload_part(summary)).column_names


def test_what_the_queue_prepared_on_its_own_disk_is_registered_there_and_once(tmp_path):
    """The `prep` of a campaign comes back: the file is registered as being on that queue, and a
    second campaign, which finds it made and returns the same row, adds nothing."""
    from ms_flow.core.data import catalog

    from amdockvs.docking.campaign import CampaignWriter, DockCampaignParams, _unprepared
    from amdockvs.runtime import AMDockVSRuntime

    runtime = AMDockVSRuntime()
    runtime.create_project(name="prepared-there", folder=tmp_path)
    try:
        db = runtime.workspace.project_db
        params = DockCampaignParams(output_dir=str(tmp_path), engine="vina", preparation_engine="ad4",
                                    hit_threshold=-8.0, hit_cap=10, executor="lab")
        elements = [{"stage": "vs", "shards": [{"shard_id": 11, "shard_path": "",
                                                "molecules": str(tmp_path / "lib.arrows")}]}]
        prep = _unprepared(elements, "ad4")
        assert [one["shards"][0]["shard_id"] for one in prep] == [11]
        where = elements[0]["shards"][0]
        assert where["prepared_as"] == "11.arrows" and "molecules" not in where

        writer = CampaignWriter(project_db=db, hit_cap=10, hit_threshold=-8.0, run_id="r", engine="vina",
                                protocol_hash="p", executor="lab", preparation_engine="ad4")
        writer.write([{"stage": "prep", "shard_id": 11, "files": [
            {"path": "/scratch/lab/ad4/11.arrows", "role": "prepared:ad4", "n_shards": 1,
             "n_records": 3, "n_active": 3, "scopes": "1"}]}])

        (row,) = [file for file in catalog.files(db, catalog.shard_spec(role="prepared:ad4"))]
        assert (row["location"], row["n_records"]) == ("lab", 3)
        assert catalog.count(db, catalog.shard_spec(role="prepared:ad4")) == 0  # nothing here opens it

        # A second campaign prepares nothing there (`_kept`) and returns the row of what is there.
        writer.write([{"stage": "prep", "shard_id": 11, "files": [
            {"path": "/scratch/lab/ad4/11.arrows", "role": "prepared:ad4", "n_shards": 1,
             "n_records": 3, "n_active": 3, "scopes": "1"}]}])
        assert [file["id"] for file in catalog.files(db, catalog.shard_spec(role="prepared:ad4"))] == [row["id"]]
    finally:
        runtime.close_project()


def test_the_far_stages_read_what_was_committed_there(tmp_path):
    """There, each task's two parts go into the key's containers (`campaign_receipts`, committed by
    the runner): the parts go, and ranking and poses read the containers."""
    from ms_flow.runner import commit_there

    from amdockvs.docking.shard_tasks import campaign_receipts

    parts = tmp_path / "out" / "results" / "k" / "parts"
    parts.mkdir(parents=True)
    a = _fragment(parts / "a.arrows", {(1, 10): -9.0, (2, 10): -7.0, (3, 10): -11.0}, shard_id=1)
    b = _fragment(parts / "b.arrows", {(4, 10): -10.0, (5, 10): -5.0}, shard_id=2)
    results = []
    for path, shard_id in ((a, 1), (b, 2)):
        result = _result(path, shard_id)
        result["envelopes"][0].update(state_key="k", generation_id=None)
        result["_committed"] = commit_there(tmp_path, campaign_receipts(result))
        results.append(result)
    assert not list(parts.iterdir())
    assert sorted(p.name.split(".")[0] for p in parts.parent.glob("*.arrows")) == ["payload_k", "summary_k"]
    # a task run again (the kill came between commit and result): the same place, its bulk from the index
    assert commit_there(tmp_path, campaign_receipts(results[0])) == results[0]["_committed"]
    assert results[0]["_committed"][0]["bulk"]["shards"] == [0]

    refs = rank(results, cap=2, threshold=-6.0)
    assert [(ref["ligand_id"], ref["receptor_id"]) for ref in refs] == [(3, 10), (4, 10)]
    [element] = by_fragment(refs)  # one summary container now
    hits = hit_poses(element)["hits"]
    assert {(hit["name"], _unpacked(hit["pose"])) for hit in hits} == {("lib:3", "pose-3-10"), ("lib:4", "pose-4-10")}
