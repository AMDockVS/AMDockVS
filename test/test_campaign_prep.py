"""A shard not prepared here is prepared by the campaign there, once, into the containers of a
folder the project keeps there, and docked from where it was left."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_unprepared_shards_are_prepared_once_and_docked_from_there(tmp_path):
    from amdockvs.docking.campaign import _unprepared
    from amdockvs.docking.shard_tasks import prepared_elements

    raw = tmp_path / "shards" / "shard_000002.arrows"
    ready = {"shard_id": 1, "shard_path": str(tmp_path / "ready.parquet")}
    pending = {"shard_id": 2, "shard_path": "", "molecules": str(raw)}
    elements = [{"receptor_id": receptor, "shards": [dict(shard)]} for receptor in (1, 2) for shard in (ready, pending)]
    prep = _unprepared(elements, "ad4")
    assert [element["shards"][0]["shard_id"] for element in prep] == [2]  # once, for both receptors
    assert all("molecules" not in shard for element in elements for shard in element["shards"])

    assert [e["shards"][0]["shard_id"] for e in prepared_elements([], elements)] == [1, 1]  # prep failed: not docked
    there = {"part": prep[0]["shards"][0]["part"], "path": str(tmp_path / "kept.arrows"), "shards": [3]}
    docked = list(prepared_elements([{"stage": "prep", "shard_id": 2, "there": there}], elements))
    assert [(e["shards"][0]["shard_path"], e["shards"][0]["prepared_shard"]) for e in docked
            if e["shards"][0]["shard_id"] == 2] == [(there["path"], 3)] * 2


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="prep-there", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def test_shards_prepared_there_go_into_one_container_the_next_campaign_finds(runtime, tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    from ms_flow.core.data import catalog
    from ms_flow.core.data.shard import Shard
    from ms_flow.runner import commit_there

    from amdockvs.docking.campaign import _unprepared
    from amdockvs.docking.preparation import tasks
    from amdockvs.docking.shard_tasks import campaign_receipts, prepare_stage, prepared_elements

    source = tmp_path / "lib.smi"
    source.write_text("".join(f"{'C' * (i + 1)}O mol{i}\n" for i in range(6)))
    states = runtime.wait_for_jobs(list(runtime.loader.shard_ligands([str(source)], executor_name="thread",
                                                                     shard_size=4)), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    rows = sorted(catalog.shards(runtime.workspace.project_db, whole=True), key=lambda row: row["base_id"])
    elements = [{"receptor_id": 1, "shards": [{
        "shard_id": row["id"], "shard_path": "", "molecules": row["path"], "shard": row["shard_index"],
        "import_batch_id": row["scope"], "generation_id": row["generation_id"]}]} for row in rows]
    prep = _unprepared(elements, "ad4")
    assert len(prep) == 2

    results = []
    for element in prep:  # as the runner there does: the task, then its receipts committed
        result = prepare_stage(element)
        result["_committed"] = commit_there(tmp_path / "campaign", campaign_receipts(result))
        results.append(result)
    kept = Path(prep[0]["shards"][0]["kept_in"])
    [container] = kept.glob("*.arrows")
    assert not [p for p in Path(prep[0]["shards"][0]["output_dir"]).iterdir() if p.is_file()]  # parts went
    docked = list(prepared_elements(results, elements))
    assert len(docked) == 2
    for element, row in zip(docked, rows):
        shard = element["shards"][0]
        assert shard["shard_path"] == str(container)
        with Shard.open(shard["shard_path"], shard=shard["prepared_shard"]) as opened:
            ids = [record.id for record in opened]
        assert ids and all(row["base_id"] <= i < row["base_id"] + row["slot_count"] for i in ids)

    def never(*_args, **_kwargs):
        raise AssertionError("prepared again")

    monkeypatch.setattr(tasks, "prepare_ligand_shards", never)
    again = prepare_stage(prep[0])  # the next campaign to this host
    assert campaign_receipts(again) == [] and again["there"] == results[0]["_committed"][0]
