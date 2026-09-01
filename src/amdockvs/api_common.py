from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ms_flow.api import FileInputSpec, ProjectOutputDirSpec

PathLike = str | Path


def normalize_ids(values: Iterable[int | str]) -> list[int]:
    seen: set[int] = set()
    resolved: list[int] = []
    for raw in values:
        value = int(raw)
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        resolved.append(value)
    return resolved


def normalize_set_name(name: str | None, *, fallback: str) -> str:
    text = str(name or "").strip()
    return text or fallback


def merge_filter_mappings(*parts: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        merged.update(dict(part))
    return merged


def normalize_files(files: Iterable[PathLike], *, label: str) -> list[Path]:
    resolved = [Path(item).expanduser().resolve() for item in files]
    if not resolved:
        raise ValueError(f"{label} requires at least one file.")
    return resolved


def group_files(files: list[Path], *, files_per_job: int) -> list[list[Path]]:
    size = max(1, int(files_per_job))
    return [files[index:index + size] for index in range(0, len(files), size)]


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


def scope_payload(scope: "MoleculeScope | None") -> dict[str, Any]:
    if scope is None:
        return {}
    return {
        "filters": dict(scope.filters or {}),
        "source_set_id": scope.source_set_id,
        "order": list(scope.order or ("id",)),
        "limit": scope.limit,
    }


@dataclass(frozen=True)
class MoleculeScope:
    filters: dict[str, Any] = field(default_factory=dict)
    source_set_id: int | None = None
    order: tuple[str, ...] = ("id",)
    limit: int | None = None


__all__ = [
    "MoleculeScope",
    "PathLike",
    "group_files",
    "json_text",
    "merge_filter_mappings",
    "normalize_files",
    "normalize_ids",
    "normalize_set_name",
    "project_root_from_output_dir",
    "worker_file",
    "worker_output_dir",
    "worker_path_fields",
    "restore_worker_paths",
]
