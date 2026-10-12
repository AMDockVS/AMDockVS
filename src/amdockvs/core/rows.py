"""Rows as a table takes them, built as plain dicts by the task that produces them.

A row heading to a sink is written as it comes: SQLModel does not run there, so what the model
would default (the timestamps) is set here. No SQL in this module: a worker loads it, and the
models hand these out as their `build_row`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def engine_state_row(*, molecule_id: int, role_type: str, engine: str, files: dict | None = None,
                     is_ready: bool = False) -> dict[str, Any]:
    now = datetime.now()
    return {
        "molecule_id": molecule_id,
        "role_type": role_type,
        "engine": engine,
        "files": files or {},
        "is_ready": is_ready,
        "created_at": now,
        "updated_at": now,
    }


def shard_engine_state_row(*, shard_id: int, engine: str, files: dict | None = None, n_records: int = 0,
                           is_ready: bool = False, error: str = "") -> dict[str, Any]:
    now = datetime.now()
    return {
        "shard_id": int(shard_id),
        "engine": str(engine),
        "files": dict(files or {}),
        "n_records": int(n_records),
        "is_ready": bool(is_ready),
        "error": str(error or ""),
        "created_at": now,
        "updated_at": now,
    }


def interaction_rows(docking_result_id: int, interactions: list[dict]) -> list[dict[str, Any]]:
    now = datetime.now()
    return [
        {
            "docking_result_id": docking_result_id,
            "interaction_type": i["interaction_type"],
            "residue": i["residue"],
            "residue_index": i.get("residue_index", 0),
            "distance": i.get("distance"),
            "geometry": i.get("geometry", {}),
            "created_at": now,
        }
        for i in interactions
    ]
