"""What a worker runs on docked poses: their interactions, their diagrams.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path
from typing import Any


from amdockvs.core.rows import interaction_rows


def detect_interactions(work: list[tuple[bytes, bytes, str]]) -> list[list[dict] | None]:
    """The interactions of each (pose, receptor, receptor_format), found by one interpreter started
    for the batch (`interactions._main`): ms_contactmap and RDKit never load where the task runs,
    so a task that only waits for it stays on a thread. None for a pose that could not be read.
    ponytail: ~0.4 s to start the interpreter, once per chunk, not per pose."""
    if not work:
        return []
    program = subprocess.Popen([sys.executable, "-m", "amdockvs.docking.results.interactions"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = program.communicate(pickle.dumps(work))
    if program.returncode:
        raise RuntimeError(f"Interaction detection exited with {program.returncode}: "
                           f"{err.decode('utf-8', 'replace')[-2000:]}")
    return pickle.loads(out)


def interactions_of_poses(payload: dict):
    """Stored poses, each with the bytes of its pose and the file of its receptor."""
    rows = [row for row in payload.get("rows") or () if row.get("pose") is not None and row.get("receptor_path")]
    receptors = {str(row["receptor_path"]): Path(str(row["receptor_path"])) for row in rows}
    data = {path: (file.read_bytes(), file.suffix.lstrip(".") or "pdb") for path, file in receptors.items()}
    found = detect_interactions([(row["pose"], *data[str(row["receptor_path"])]) for row in rows])
    rows_out: list[dict[str, Any]] = []
    for row, interactions in zip(rows, found):
        if interactions is not None:
            rows_out.extend(interaction_rows(row["receptor_payload_id"], row["pose_payload_id"], interactions))
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
