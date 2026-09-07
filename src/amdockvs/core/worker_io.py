"""Declaring the files and directories a compute worker may touch.

Jobs run out-of-process (and possibly off-host), so a path in a chunk payload is
not a path: it is a transfer declaration that MolSuite materialises worker-side.
These wrappers are the only place that knows it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ms_flow.api import FileInputSpec, ProjectOutputDirSpec


def worker_file(path: Any, *, cache: bool = False):
    """Declare an existing file path that a compute worker must be able to open."""
    text = str(path or "").strip()
    if not text or not Path(text).expanduser().is_file():
        return text
    return FileInputSpec(text, delivery="path", cache=cache)


def worker_path_fields(
    row: Mapping[str, Any],
    *fields: str,
    cached: Iterable[str] = (),
) -> dict[str, Any]:
    result = dict(row)
    cached_fields = set(cached)
    logical: dict[str, str] = {}
    for field_name in fields:
        text = str(result.get(field_name) or "").strip()
        if not text:
            continue
        logical[field_name] = text
        result[field_name] = worker_file(text, cache=field_name in cached_fields)
    if logical:
        result["_worker_logical_paths"] = logical
    return result


def restore_worker_paths(value: Any, rows: Iterable[Mapping[str, Any]]) -> Any:
    replacements = {
        str(row.get(field_name) or ""): logical
        for row in rows
        for field_name, logical in dict(row.get("_worker_logical_paths") or {}).items()
        if str(row.get(field_name) or "")
    }

    def restore(item: Any) -> Any:
        if isinstance(item, str):
            return replacements.get(item, item)
        if isinstance(item, dict):
            return {key: restore(child) for key, child in item.items()}
        if isinstance(item, list):
            return [restore(child) for child in item]
        return item

    return restore(value)


def worker_output_dir(path: str | Path) -> ProjectOutputDirSpec:
    return ProjectOutputDirSpec(str(Path(path).expanduser().resolve()))


def project_root_from_output_dir(path: str | Path) -> Path:
    """Resolve the project-shaped temporary root used by transferred Ray outputs."""
    resolved = Path(path).expanduser().resolve()
    markers = {"data", "results", "exports", "jobs"}
    indexes = [index for index, part in enumerate(resolved.parts) if part in markers]
    return Path(*resolved.parts[: max(indexes)]) if indexes else resolved.parent


def json_text(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=True)


__all__ = [
    "json_text",
    "project_root_from_output_dir",
    "restore_worker_paths",
    "worker_file",
    "worker_output_dir",
    "worker_path_fields",
]
