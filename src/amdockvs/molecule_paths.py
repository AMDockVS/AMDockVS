from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


_DEFAULT_PROJECT_ROOT: Path | None = None


def normalize_path(raw_path: str | Path | None) -> Path | None:
    text = str(raw_path or "").strip()
    return None if not text else Path(text).expanduser().resolve()


def set_default_project_root(project_root: str | Path | None) -> None:
    global _DEFAULT_PROJECT_ROOT
    _DEFAULT_PROJECT_ROOT = None if project_root is None else Path(project_root).expanduser().resolve()


def get_default_project_root() -> Path | None:
    return _DEFAULT_PROJECT_ROOT


def _project_root_from_row(row: Mapping[str, Any] | Any) -> Path | None:
    extra_data = _value(row, "extra_data")
    if isinstance(extra_data, str):
        try:
            extra_data = json.loads(extra_data)
        except json.JSONDecodeError:
            extra_data = None
    if isinstance(extra_data, Mapping):
        project_root = extra_data.get("project_root")
        if project_root:
            return Path(str(project_root)).expanduser().resolve()
    return _DEFAULT_PROJECT_ROOT


def _resolve_path_for_row(row: Mapping[str, Any] | Any, key: str) -> Path | None:
    raw_value = _value(row, key)
    text = str(raw_value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    project_root = _project_root_from_row(row)
    if project_root is not None:
        return (project_root / path).resolve()
    return path.resolve()


def chemistry_current_path_from_metadata(raw_metadata: str | None) -> Path | None:
    if not raw_metadata:
        return None
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    chemistry = metadata.get("chemistry")
    if not isinstance(chemistry, dict):
        return None
    current = chemistry.get("current")
    if not isinstance(current, dict):
        return None
    return normalize_path(current.get("path"))


def preferred_molecule_path(row: Mapping[str, Any] | Any) -> Path | None:
    current_path = _resolve_path_for_row(row, "current_path")
    if current_path is not None and current_path.exists():
        return current_path
    metadata_path = chemistry_current_path_from_metadata(str(_value(row, "metadata_json") or ""))
    if metadata_path is not None and metadata_path.exists():
        return metadata_path
    stored_path = _resolve_path_for_row(row, "stored_path")
    if stored_path is not None and stored_path.exists():
        return stored_path
    return current_path or metadata_path or stored_path


def current_molecule_path(row: Mapping[str, Any] | Any) -> Path | None:
    current_path = _resolve_path_for_row(row, "current_path")
    stored_path = _resolve_path_for_row(row, "stored_path")
    if current_path is not None and current_path.exists():
        return current_path
    return stored_path or current_path


def stored_molecule_path(row: Mapping[str, Any] | Any) -> Path | None:
    stored_path = _resolve_path_for_row(row, "stored_path")
    current_path = _resolve_path_for_row(row, "current_path")
    if stored_path is not None and stored_path.exists():
        return stored_path
    return current_path or stored_path


def molecule_storage_key(kind: str, source_file: Path, source_index: int) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_]+", "_", source_file.stem)[:32].strip("_") or "item"
    seed = f"{source_file.resolve()}::{source_index}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{str(kind).strip().lower()}_{stem}_{int(source_index):09d}_{digest}"


def infer_storage_key(path: str | Path) -> str:
    stem = Path(path).expanduser().resolve().stem
    if "__" in stem:
        stem = stem.split("__", 1)[0]
    if stem.endswith("_o"):
        stem = stem[:-2]
    return stem


def role_storage_root(storage_root: str | Path, *, role: str) -> Path:
    root = Path(storage_root).expanduser().resolve() / str(role).strip().lower()
    root.mkdir(parents=True, exist_ok=True)
    return root


def shard_name_for_key(key: str) -> str:
    digest = hashlib.sha1(str(key).encode("utf-8")).hexdigest()
    return digest[:2]


def original_storage_path(storage_root: str | Path, *, role: str, key: str, suffix: str) -> Path:
    root = role_storage_root(storage_root, role=role) / "original" / shard_name_for_key(key)
    root.mkdir(parents=True, exist_ok=True)
    normalized_suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
    return root / f"{key}_o{normalized_suffix}"


def current_storage_path(storage_root: str | Path, *, role: str, key: str, suffix: str) -> Path:
    root = role_storage_root(storage_root, role=role) / "current" / shard_name_for_key(key)
    root.mkdir(parents=True, exist_ok=True)
    normalized_suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
    return root / f"{key}{normalized_suffix}"


def artifact_storage_path(
    storage_root: str | Path,
    *,
    role: str,
    key: str,
    artifact_name: str,
    suffix: str,
) -> Path:
    root = role_storage_root(storage_root, role=role) / "artifacts" / shard_name_for_key(key)
    root.mkdir(parents=True, exist_ok=True)
    normalized_suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
    safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(artifact_name).strip().lower()).strip("_") or "artifact"
    return root / f"{key}__{safe_name}{normalized_suffix}"


def managed_paths_for_source(
    storage_root: str | Path,
    *,
    role: str,
    source_file: Path,
    source_index: int,
    original_suffix: str,
    current_suffix: str | None = None,
) -> dict[str, Path | str]:
    key = molecule_storage_key(str(role), source_file, source_index)
    return {
        "key": key,
        "original_path": original_storage_path(storage_root, role=role, key=key, suffix=original_suffix),
        "current_path": current_storage_path(
            storage_root,
            role=role,
            key=key,
            suffix=original_suffix if current_suffix is None else current_suffix,
        ),
    }


def current_path_for_existing(
    storage_root: str | Path,
    *,
    role: str,
    source_path: str | Path,
    suffix: str | None = None,
) -> Path:
    resolved = Path(source_path).expanduser().resolve()
    key = infer_storage_key(resolved)
    normalized_suffix = resolved.suffix if suffix is None else suffix
    return current_storage_path(storage_root, role=role, key=key, suffix=normalized_suffix)


def artifact_path_for_existing(
    storage_root: str | Path,
    *,
    role: str,
    source_path: str | Path,
    artifact_name: str,
    suffix: str,
) -> Path:
    resolved = Path(source_path).expanduser().resolve()
    key = infer_storage_key(resolved)
    return artifact_storage_path(
        storage_root,
        role=role,
        key=key,
        artifact_name=artifact_name,
        suffix=suffix,
    )


def _value(row: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


__all__ = [
    "artifact_path_for_existing",
    "artifact_storage_path",
    "chemistry_current_path_from_metadata",
    "current_path_for_existing",
    "current_storage_path",
    "infer_storage_key",
    "managed_paths_for_source",
    "molecule_storage_key",
    "normalize_path",
    "original_storage_path",
    "preferred_molecule_path",
    "current_molecule_path",
    "role_storage_root",
    "shard_name_for_key",
    "stored_molecule_path",
    "set_default_project_root",
    "get_default_project_root",
]
