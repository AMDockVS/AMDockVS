"""What a worker runs on docked poses: their interactions, their diagrams.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ms_flow.payloads import scratch_file

from amdockvs.core.rows import interaction_rows
from amdockvs.core.worker_io import project_root_from_output_dir
from amdockvs.docking.results.interactions import collect_interaction_rows


def _relative_to_project(path: Path, project_root: Path | None) -> str:
    if project_root is not None:
        try:
            return str(path.resolve().relative_to(project_root.resolve()))
        except Exception:
            pass
    return str(path)


def _write_interaction_report(
    *,
    output_dir: Path,
    project_root: Path | None,
    result_id: int,
    row: Mapping[str, Any],
    method: str,
    interactions: list[dict[str, Any]],
) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"result_{int(result_id):09d}.interactions.json"
    payload = {
        "schema": "amdockvs.interactions.v1",
        "generated_at": datetime.now().isoformat(),
        "method": method,
        "docking_result_id": int(result_id),
        "receptor_molecule_id": int(row.get("receptor_molecule_id") or 0),
        "ligand_molecule_id": int(row.get("ligand_molecule_id") or 0),
        "engine": str(row.get("engine") or ""),
        "pose_rank": int(row.get("pose_rank") or 1),
        "score": row.get("score"),
        "pose_payload_id": row.get("pose_payload_id"),
        "receptor_payload_id": (row.get("metrics") or {}).get("receptor_payload_id"),
        "interaction_count": len(interactions),
        "interactions": interactions,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    return _relative_to_project(path, project_root)


def interactions_of_poses(payload: dict):
    rows_out: list[dict[str, Any]] = []
    method = str(payload.get("method") or "ms_contactmap")
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    project_root = project_root_from_output_dir(output_dir)
    for row in list(payload.get("rows") or []):
        result_id = int(row.get("id") or 0)
        if result_id <= 0:
            continue
        pose = row.get("pose")
        interactions = []
        if pose is not None:
            with scratch_file(pose, ".sdf") as pose_path:
                interactions = collect_interaction_rows(
                    pose_path=pose_path, receptor_path=str(row.get("receptor_path") or "")
                )
        json_path = _write_interaction_report(
            output_dir=output_dir,
            project_root=project_root,
            result_id=result_id,
            row=row,
            method=method,
            interactions=interactions,
        )
        if interactions:
            for interaction in interactions:
                geometry = dict(interaction.get("geometry") or {})
                geometry["json_path"] = json_path
                interaction["geometry"] = geometry
            rows_out.extend(interaction_rows(result_id, interactions))
        else:
            rows_out.extend(
                interaction_rows(
                    result_id,
                    [
                        {
                            "interaction_type": "none",
                            "residue": "",
                            "residue_index": 0,
                            "distance": None,
                            "geometry": {
                                "json_path": json_path,
                                "method": method,
                                "interaction_count": 0,
                            },
                        }
                    ],
                )
            )
    return rows_out


def diagrams_of_poses(payload: dict):
    from amdockvs.docking.results.diagram import render_diagrams_for_result_rows

    rows = list(payload.get("rows") or [])
    written = render_diagrams_for_result_rows(
        rows,
        fmt=str(payload.get("fmt") or "png"),
        replace_existing=bool(payload.get("replace_existing")),
        output_dir=str(payload.get("output_dir") or "") or None,
    )
    # The renderer swallows per-row failures so a diagram never breaks a docking run. That
    # is right for the inline hook and wrong here: a standalone job that rendered nothing
    # reported "completed" and the user had no way to learn the renderer was missing.
    if rows and not written:
        raise RuntimeError(
            f"No diagram could be rendered for any of the {len(rows)} poses in this chunk."
        )
    return []
