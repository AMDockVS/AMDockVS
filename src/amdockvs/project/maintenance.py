"""Keeping a project's payload volumes healthy — pure/off-thread-safe (no Qt).

Both run in the background and neither raises on a project without payloads: the check when a
project opens (fast: it reads the index, not the bytes) and the compaction when the last job ends,
once enough of a volume is dead bytes (D6 of the volumes plan).
"""
from __future__ import annotations

from amdockvs.core.payloads import payload_store

# Share of a sealed volume's bytes that no row covers before copying the rest is worth it.
MIN_DEAD = 0.5


def check_storage(project_db) -> tuple[str, str, str]:
    """(level, title, text) of a fast `check`, for the notification log: `level` is its
    "info" | "warning" | "error". Errors are bytes that cannot be read; warnings are garbage a repair clears."""
    report = payload_store(project_db).check()
    if not report.ok:
        text = f"{len(report.broken)} stored structures cannot be read"
        if report.missing_dicts:
            text += f" and {len(report.missing_dicts)} compression dictionaries are missing"
        return "error", "Project storage is damaged", text + "."
    garbage = len(report.unreferenced) + len(report.empty) + len(report.orphans)
    if garbage:
        return "warning", "Project storage has leftovers", f"{garbage} unused entries can be cleared by a repair."
    return "info", "Project storage checked", "Nothing to repair."


def compact_if_idle(project_db, *, busy: bool, min_dead: float = MIN_DEAD) -> dict | None:
    """Compact the volumes that are `min_dead` dead or more, unless a job is running. Returns the
    stats of the compaction, or None when nothing was due. Merging small volumes is left to the
    explicit button: it would run after every job."""
    if busy:
        return None
    store = payload_store(project_db)
    if not any(dead >= min_dead for dead in store.check().dead.values()):
        return None
    return store.compact(min_dead, small=0)
