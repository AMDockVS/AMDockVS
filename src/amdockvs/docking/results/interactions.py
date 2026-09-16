from __future__ import annotations

from pathlib import Path
from typing import Any


# ms_contactmap's vocabulary is ours except for these two names; everything else
# ("hydrophobic", "salt_bridge", "pi_stacking", "pi_cation", "halogen_bond",
# "water_bridge") passes through unchanged.
_INTERACTION_ALIASES = {
    "hbond": "hydrogen_bond",
    "metal_coordination": "metal_complex",
}


def _interaction_type(name: str) -> str:
    key = str(name or "").strip().lower()
    return _INTERACTION_ALIASES.get(key, key or "interaction")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except Exception:
        pass
    return str(value)


def collect_interaction_rows(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    pose_rank: int = 1,
) -> list[dict]:
    """Protein-ligand interactions for one pose, from ms_contactmap's native detector.

    Single provider by design: ms_contactmap is ours, so a second detector would only
    add a vocabulary to reconcile and a licence to inherit. The diagram viewer builds
    the very same :class:`~ms_contactmap.Diagram`, so the table and the drawing can
    never disagree about what was detected.
    """
    from amdockvs.docking.results.diagram import build_pose_diagram

    diagram = build_pose_diagram(pose_path=pose_path, receptor_path=receptor_path, pose_rank=pose_rank)
    if diagram is None:
        return []
    residues = {residue.key: residue.ref for residue in diagram.residues}
    rows: list[dict[str, Any]] = []
    for interaction in diagram.interactions:
        ref = residues.get(interaction.residue_key)
        if ref is None:  # an interaction whose residue never made it into the diagram
            continue
        distance = float(getattr(interaction, "distance", 0.0) or 0.0)
        rows.append(
            {
                "interaction_type": _interaction_type(interaction.kind),
                "residue": f"{ref.name}{ref.number}:{ref.chain}",
                "residue_index": int(ref.number or 0),
                "distance": round(distance, 3) if distance else None,
                "geometry": {
                    "method": "ms_contactmap",
                    "provider": "ms_contactmap",
                    "provider_interaction": str(interaction.kind),
                    "provider_payload": _json_safe(
                        {
                            "ligand_is_donor": interaction.ligand_is_donor,
                            "protein_atom": interaction.protein_atom,
                            "angle": interaction.angle,
                            "via_water": interaction.via_water,
                        }
                    ),
                },
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row["interaction_type"]),
            int(row.get("residue_index") or 0),
            str(row["residue"]),
        ),
    )


__all__ = ["collect_interaction_rows"]
