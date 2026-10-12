"""What a worker runs to dock a chunk of pairs.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from amdockvs.docking.engines.registry import run_docking_chunk


def dock_pairs(payload: dict, progress_cb=None):
    rows = run_docking_chunk(payload, progress_cb)
    threshold = payload.get("hit_threshold")
    if threshold is None:
        return rows
    # The predicate half of the gate: stateless, so it runs where the poses are produced
    # and the non-hits never travel back. ponytail: their pose files stay on disk and the
    # pair has no row, so a re-run with skip_existing will dock it again — the campaign is
    # capped anyway; add a "screened" marker table if resuming a capped run matters.
    return [
        row for row in rows
        if row.get("score") is not None and float(row["score"]) <= float(threshold)
    ]
