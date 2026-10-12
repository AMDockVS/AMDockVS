"""Rows as a table takes them, built as plain dicts by the task that produces them.

A row heading to a sink is written as it comes: SQLModel does not run there, so what the model
would default (the timestamps) is set here. No SQL in this module: a worker loads it, and the
models hand these out as their `build_row`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from amdockvs.core.vocab import BindingSiteSource, FileFormat, ModelSource, MoleculeType, MoleculeUsageClass


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


_RESERVED_EXTRA_DATA_KEYS = {"state", "project_root", "filtering", "chemistry"}


def sanitize_molecule_extra_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        raw_mapping = dict(value)
    else:
        text_value = str(value or "").strip()
        if not text_value:
            return {}
        try:
            parsed = json.loads(text_value)
        except json.JSONDecodeError:
            return {}
        raw_mapping = parsed if isinstance(parsed, dict) else {}
    return {
        str(key): item
        for key, item in raw_mapping.items()
        if str(key) not in _RESERVED_EXTRA_DATA_KEYS
    }


def molecule_row(
        *,
        project_root: Path,
        source_file: Path,
        source_index: int,
        name: str,
        molecule_type: str,
        n_atoms: int,
        input_format: str,
        stored_path: Path | None,
        current_path: Path | None = None,
        current_model_index: int | None = None,
        extra_data: dict | None = None,
        created_at: datetime | None = None,
        primary_context: str = "",
        usage_class: str = MoleculeUsageClass.GENERAL,
) -> dict[str, Any]:
    now = created_at or datetime.now()
    raw_meta = dict(extra_data or {}) if isinstance(extra_data, dict) else {}
    state = raw_meta.get("state", {}) if isinstance(raw_meta.get("state"), dict) else {}
    meta = sanitize_molecule_extra_data(extra_data)
    return {
        "name": str(name or f"{source_file.stem}_{source_index}"),
        "molecule_type": str(molecule_type or MoleculeType.UNKNOWN),
        "source": str(source_file.resolve()),
        "source_index": max(0, source_index),
        "input_format": str(input_format or FileFormat.UNKNOWN),
        # A sequence-only protein has no file yet: both paths stay "" until a model is predicted.
        "stored_path": str(stored_path.relative_to(project_root)) if stored_path else "",
        "current_path": str((current_path or stored_path).relative_to(project_root)) if stored_path else "",
        "current_model_index": None if current_model_index is None else int(current_model_index),
        "active_binding_site_id": None,
        "n_atoms": max(0, n_atoms),
        "mw": meta.get("mw"),
        "exact_mw": None,
        "logp": None,
        "hbd": None,
        "hba": None,
        "tpsa": None,
        "rotatable_bonds": None,
        "fragment_count": None,
        "ring_count": None,
        "aromatic_ring_count": None,
        "hetero_atom_count": None,
        "heavy_atom_count": None,
        "formal_charge": None,
        "fraction_csp3": None,
        "pains_matches": [],
        "ro5_violations": [],
        "conformer_count": max(0, int(state.get("conformer_count", 0) or 0)),
        "has_3d": bool(state.get("has_3d", False)),
        "has_hs": bool(state.get("has_hs", False)),
        "is_minimized": False,
        "has_activity": False,
        "excluded": False,
        "exclusion_reason": "",
        "in_set": False,
        "usage_class": str(usage_class or MoleculeUsageClass.GENERAL),
        "primary_context": primary_context,
        "extra_data": meta,
        "created_at": now,
        "updated_at": now,
    }


def molecule_model_row(
        *,
        molecule_id: int,
        model_index: int,
        file_path: str,
        source: str = ModelSource.IMPORTED,
        energy: float | None = None,
        metrics: dict | None = None,
        files: dict | None = None,
) -> dict[str, Any]:
    return {
        "molecule_id": molecule_id,
        "model_index": model_index,
        "file_path": file_path,
        "energy": energy,
        "source": source,
        "metrics": dict(metrics or {}),
        "files": dict(files or {}),
        "created_at": datetime.now(),
    }


def binding_site_row(
        *,
        molecule_id: int,
        name: str = "",
        source: str = BindingSiteSource.MANUAL,
        source_ref: str = "",
        center: tuple[float, float, float] | None = None,
        size: tuple[float, float, float] | None = None,
        extra_data: dict | None = None,
) -> dict[str, Any]:
    # A row heading to a sink is written as-is: SQLModel does not run here, so the
    # default_factory of created_at (NOT NULL) never fires. build_row sets it.
    cx, cy, cz = center or (None, None, None)
    sx, sy, sz = size or (None, None, None)
    return {
        "molecule_id": molecule_id,
        "name": name,
        "source": source,
        "source_ref": source_ref,
        "center_x": cx, "center_y": cy, "center_z": cz,
        "size_x": sx, "size_y": sy, "size_z": sz,
        "extra_data": extra_data,
        "created_at": datetime.now(),
    }
