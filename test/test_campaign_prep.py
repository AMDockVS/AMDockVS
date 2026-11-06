"""A shard not prepared here is prepared by the campaign there, once, and docked from where it was left."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_unprepared_shards_are_prepared_once_and_docked_from_there(tmp_path):
    from amdockvs.docking.campaign import _unprepared
    from amdockvs.docking.shard_tasks import prepared_elements

    raw = tmp_path / "shards" / "shard_000002.parquet"
    ready = {"shard_id": 1, "shard_path": str(tmp_path / "ready.parquet")}
    pending = {"shard_id": 2, "shard_path": "", "molecules": str(raw)}
    elements = [{"receptor_id": receptor, "shards": [dict(shard)]} for receptor in (1, 2) for shard in (ready, pending)]
    prep = _unprepared(elements, "ad4")
    assert [element["shards"][0]["shard_id"] for element in prep] == [2]  # once, for both receptors
    assert all("molecules" not in shard for element in elements for shard in element["shards"])

    (tmp_path / "ready.parquet").write_bytes(b"")
    assert [e["shards"][0]["shard_id"] for e in prepared_elements([], elements)] == [1, 1]  # prep failed: not docked
    twin = Path(prep[0]["shards"][0]["output_dir"]) / raw.name
    twin.parent.mkdir(parents=True)
    twin.write_bytes(b"")
    docked = list(prepared_elements([], elements))
    assert [e["shards"][0]["shard_path"] for e in docked if e["shards"][0]["shard_id"] == 2] == [str(twin)] * 2
