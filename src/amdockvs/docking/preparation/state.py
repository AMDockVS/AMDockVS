"""What a docking run reads of an entity's preparation: the input payload and the grid.

The prepared PDBQT is `EngineState.prepared_payload_id`, hydrated onto the row as
`prepared_payload_id`; the grid lives in a per-engine slot of the metadata JSON.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping

from amdockvs.core.payloads import molecule_payload_id


def _decode_metadata(raw_metadata: str | None) -> dict[str, Any]:
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_metadata(metadata: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata or {}), ensure_ascii=True)


def docking_input_payload_id(row: Mapping[str, Any]) -> int:
    """What an engine docks: the prepared input, else the structure itself."""
    payload_id = row.get("prepared_payload_id") or molecule_payload_id(row)
    if not payload_id:
        raise ValueError(f"Molecule {row.get('id')} has no structure to dock.")
    return int(payload_id)


def grid_from_metadata_json(raw_metadata: str | None, *, engine: str = "ad4") -> dict[str, Any] | None:
    metadata = _decode_metadata(raw_metadata)
    grids = dict(metadata.get("docking_grids") or {})
    entry = grids.get(str(engine).strip().lower())
    return dict(entry) if isinstance(entry, dict) else None


def grid_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> dict[str, Any] | None:
    payload = row.get("grid_engine_payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    payload = row.get(f"grid_{str(engine).strip().lower()}_payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return grid_from_metadata_json(row.get("metadata_json"), engine=engine)


def merge_grid_metadata(
    raw_metadata: str | None,
    *,
    engine: str,
    center: Iterable[float],
    size: Iterable[float],
    spacing: float,
    extra: Mapping[str, Any] | None = None,
) -> str:
    metadata = _decode_metadata(raw_metadata)
    grids = dict(metadata.get("docking_grids") or {})
    entry = {
        "engine": str(engine),
        "center": [float(value) for value in center],
        "size": [float(value) for value in size],
        "spacing": float(spacing),
        "created_at": datetime.now().isoformat(),
    }
    if extra:
        entry.update(dict(extra))
    grids[str(engine).strip().lower()] = entry
    metadata["docking_grids"] = grids
    return _encode_metadata(metadata)


__all__ = [
    "docking_input_payload_id",
    "grid_from_metadata_json",
    "grid_from_row",
    "merge_grid_metadata",
]
