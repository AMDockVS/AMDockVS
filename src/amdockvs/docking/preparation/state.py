"""What a prepared entity stores in its `EngineState` metadata JSON.

One engine's preparation leaves two things behind: the path of the file it wrote and,
for a receptor, the grid it was prepared for. Both live in a per-engine slot of the
same metadata blob, so reading and merging them is one place and not five.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from amdockvs.core.paths import normalize_path, preferred_molecule_path


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


def _prepared_entry(metadata: Mapping[str, Any] | None, *, engine: str) -> dict[str, Any] | None:
    prepared = dict((metadata or {}).get("prepared") or {})
    entry = prepared.get(str(engine).strip().lower())
    return dict(entry) if isinstance(entry, dict) else None


def prepared_path_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> Path | None:
    direct_path = str(row.get(f"prepared_{str(engine).strip().lower()}_path") or "").strip()
    if direct_path:
        return normalize_path(direct_path)
    files = dict(row.get("prepared_files") or {})
    if files:
        direct = str(files.get("prepared") or "").strip()
        if direct:
            return normalize_path(direct)
    metadata = _decode_metadata(row.get("metadata_json"))
    entry = _prepared_entry(metadata, engine=engine)
    if entry is None:
        return None
    return normalize_path(entry.get("path"))


def docking_input_path_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> Path:
    prepared_path = prepared_path_from_row(row, engine=engine)
    if prepared_path is not None:
        return prepared_path
    stored_path = preferred_molecule_path(row)
    if stored_path is None:
        raise ValueError(f"Missing stored_path for docking input row: {dict(row)}")
    return stored_path


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


def merge_prepared_metadata(
    raw_metadata: str | None,
    *,
    engine: str,
    path: Path,
    source_path: Path,
    entity_kind: str,
    extra: Mapping[str, Any] | None = None,
) -> str:
    metadata = _decode_metadata(raw_metadata)
    prepared = dict(metadata.get("prepared") or {})
    entry = {
        "engine": str(engine),
        "entity_kind": str(entity_kind),
        "path": str(path),
        "source_path": str(source_path),
        "created_at": datetime.now().isoformat(),
    }
    if extra:
        entry.update(dict(extra))
    prepared[str(engine).strip().lower()] = entry
    metadata["prepared"] = prepared
    return _encode_metadata(metadata)


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
    "docking_input_path_from_row",
    "grid_from_metadata_json",
    "grid_from_row",
    "merge_grid_metadata",
    "merge_prepared_metadata",
    "prepared_path_from_row",
]
