"""Argument normalisation shared by the feature APIs.

Pure, dependency-free helpers: every public `api.py` accepts loose user input
(ids as str, empty names, a bag of filters) and needs the same coercions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

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


__all__ = [
    "PathLike",
    "group_files",
    "merge_filter_mappings",
    "normalize_files",
    "normalize_ids",
    "normalize_set_name",
]
