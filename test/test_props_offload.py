from __future__ import annotations

from pathlib import Path

from amdockvs.io.properties import PROPERTIES_SUBDIR, PROPS_SHARD_KEY, read_source_properties
from amdockvs.io.transformers.materializers import offload_source_properties


def _row(source_index: int, props: dict[str, str]) -> dict:
    return {
        "source_index": source_index,
        "source_properties": [{"key": k, "value_text": v} for k, v in props.items()],
        "extra_data": {"smiles": "CCO"},
    }


def test_offload_round_trip(tmp_path: Path):
    rows = [
        _row(0, {"MW": "46.07", "LogP": "-0.14", "Name": "ethanol"}),
        _row(1, {"MW": "18.02", "Name": "water"}),
    ]
    shard = offload_source_properties(rows, tmp_path)
    assert shard is not None

    props_dir = tmp_path / PROPERTIES_SUBDIR
    assert (props_dir / shard).exists()
    # rows are stripped and carry the shard ref so the sink emits no props node
    for row in rows:
        assert row["source_properties"] == []
        assert row["extra_data"][PROPS_SHARD_KEY] == shard

    # each molecule reads back ONLY its own tags (filtered by source_index)
    assert read_source_properties(props_dir, shard, 0) == {"MW": "46.07", "LogP": "-0.14", "Name": "ethanol"}
    assert read_source_properties(props_dir, shard, 1) == {"MW": "18.02", "Name": "water"}


def test_offload_no_properties_is_noop(tmp_path: Path):
    rows = [{"source_index": 0, "source_properties": [], "extra_data": {}}]
    assert offload_source_properties(rows, tmp_path) is None
    assert not (tmp_path / PROPERTIES_SUBDIR).exists()


def test_read_missing_shard_returns_empty(tmp_path: Path):
    assert read_source_properties(tmp_path, None, 0) == {}
    assert read_source_properties(tmp_path, "does-not-exist.parquet", 0) == {}
