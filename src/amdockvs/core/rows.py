"""Rows as a table takes them, built as plain dicts by the task that produces them.

A row heading to a sink is written as it comes: SQLModel does not run there, so nothing the model
would default is filled in. A `*_payload_id` field takes an id or an
`ms_flow.payloads.Payload`: the sink stores the bytes and writes the id in the same transaction.
No SQL in this module: a worker loads it, and the models hand these out as their `build_row`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amdockvs.core.vocab import (
    BindingSiteSource, ClusteringMethod, ModelSource, MoleculeType, MoleculeUsageClass,
)


def prep_row(*, role_type: str, molecule_id: int, family: str, payload_id: Any = None,
             flex_payload_id: Any = None, error: str | None = None) -> dict[str, Any]:
    """A `receptor_preps` or `ligand_preps` row. No `payload_id` = it failed, `error` says why."""
    if role_type == "receptor":
        return {"receptor_molecule_id": molecule_id, "family": family, "rigid_payload_id": payload_id,
                "flex_payload_id": flex_payload_id, "error": error}
    return {"molecule_id": molecule_id, "family": family, "payload_id": payload_id, "error": error}


def shard_prep_row(*, shard_id: int, family: str, n_records: int = 0, n_failed: int = 0) -> dict[str, Any]:
    return {"shard_id": int(shard_id), "family": str(family), "n_records": int(n_records), "n_failed": int(n_failed)}


def interaction_rows(receptor_payload_id: Any, pose_payload_id: Any, interactions: list[dict]) -> list[dict[str, Any]]:
    """The rows of one pose in its receptor. No interactions is one "none" row: the pose was looked at."""
    interactions = interactions or [{"interaction_type": "none", "residue": "", "geometry": {"method": "ms_contactmap"}}]
    return [
        {
            "receptor_payload_id": receptor_payload_id,
            "pose_payload_id": pose_payload_id,
            "interaction_type": i["interaction_type"],
            "residue": i["residue"],
            "residue_index": i.get("residue_index", 0),
            "distance": i.get("distance"),
            "geometry": i.get("geometry", {}),
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
        source_file: Path,
        source_index: int,
        name: str,
        molecule_type: str,
        n_atoms: int,
        import_batch_id: int | None = None,
        current_payload_id: Any = None,
        stored_payload_id: Any = None,
        extra_data: dict | None = None,
        usage_class: str = MoleculeUsageClass.GENERAL,
) -> dict[str, Any]:
    raw_meta = dict(extra_data or {}) if isinstance(extra_data, dict) else {}
    state = raw_meta.get("state", {}) if isinstance(raw_meta.get("state"), dict) else {}
    meta = sanitize_molecule_extra_data(extra_data)
    return {
        "name": str(name or f"{source_file.stem}_{source_index}"),
        "molecule_type": str(molecule_type or MoleculeType.UNKNOWN),
        "import_batch_id": import_batch_id,
        "source_index": max(0, source_index),
        # A sequence-only protein has no structure yet: both stay None until a model is predicted.
        # stored only when the input differs from the current form (D11).
        "stored_payload_id": stored_payload_id,
        "current_payload_id": current_payload_id,
        "n_atoms": max(0, n_atoms),
        "inchikey": None,
        "canonical_smiles": None,
        "mw": meta.get("mw"),
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
        "conformer_count": max(0, int(state.get("conformer_count", 0) or 0)),
        "has_3d": bool(state.get("has_3d", False)),
        "has_hs": bool(state.get("has_hs", False)),
        "is_minimized": False,
        "excluded": False,
        "exclusion_reason": "",
        "in_set": False,
        "usage_class": str(usage_class or MoleculeUsageClass.GENERAL),
        "extra_data": meta,
    }


def molecule_model_row(
        *,
        molecule_id: int,
        payload_id: Any,
        source: str = ModelSource.IMPORTED,
        data_payload_id: Any = None,
) -> dict[str, Any]:
    return {
        "molecule_id": molecule_id,
        "payload_id": payload_id,
        "source": source,
        "data_payload_id": data_payload_id,
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
        is_active: bool = False,
) -> dict[str, Any]:
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
        "is_active": bool(is_active),
    }


def clustering_rows(
    cluster_run_id: str,
    assignments: list[dict],  # [{"molecule_id": int, "cluster_id": int, "is_centroid": bool}]
    method: str = ClusteringMethod.BUTINA,
    fp_type: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "molecule_id": a["molecule_id"],
            "cluster_run_id": cluster_run_id,
            "cluster_id": a["cluster_id"],
            "is_centroid": a.get("is_centroid", False),
            "method": method,
            "fp_type": fp_type,
        }
        for a in assignments
    ]
