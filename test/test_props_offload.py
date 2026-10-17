from __future__ import annotations

from pathlib import Path

from ms_flow.core.database.project import ProjectStore

from amdockvs.core.constants import SERIES_PROPERTIES
from amdockvs.io.properties import read_source_properties
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
    payload = offload_source_properties(rows)
    assert (payload.kind, payload.series) == ("parquet", SERIES_PROPERTIES)
    # rows are stripped and share ONE payload, so the sink stores the batch's tags once
    for row in rows:
        assert row["source_properties"] == []
        assert row["properties_payload_id"] is payload

    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        (payload_id,) = store.payloads.put(session, [payload])
        session.commit()
    assert not list(tmp_path.glob("**/*.parquet"))  # in the volumes, not a file of its own

    # each molecule reads back ONLY its own tags (filtered by source_index)
    assert read_source_properties(store, payload_id, 0) == {"MW": "46.07", "LogP": "-0.14", "Name": "ethanol"}
    assert read_source_properties(store, payload_id, 1) == {"MW": "18.02", "Name": "water"}
    assert read_source_properties(store, payload_id, 7) == {}


def test_offload_no_properties_is_noop():
    rows = [{"source_index": 0, "source_properties": [], "extra_data": {}}]
    assert offload_source_properties(rows) is None
    assert "properties_payload_id" not in rows[0]


def test_read_without_payload_returns_empty(tmp_path: Path):
    assert read_source_properties(tmp_path, None, 0) == {}
