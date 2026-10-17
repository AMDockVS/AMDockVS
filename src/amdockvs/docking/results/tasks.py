"""What a worker runs on docked poses: their interactions, their diagrams.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from typing import Any

from ms_flow.payloads import scratch_file

from amdockvs.core.rows import interaction_rows
from amdockvs.docking.results.interactions import collect_interaction_rows


def interactions_of_poses(payload: dict):
    rows_out: list[dict[str, Any]] = []
    method = str(payload.get("method") or "ms_contactmap")
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
        if interactions:
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
