"""What a project's structure volumes hold, for the storage panel: size on disk, dead bytes
(structures that were deleted and not yet compacted away), a health check and a compaction.
And the upkeep that runs on its own: the fast check when a project opens (it reads the index,
not the bytes) and the compaction when the last job ends, once enough of a volume is dead bytes
(D6 of the volumes plan).

Blocking and Qt-free: the UI runs them with `run_async`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ms_flow.payloads import HEADER, PayloadStore, Report

from amdockvs.core.payloads import payload_store

# Share of a sealed volume's bytes that no row covers before copying the rest is worth it.
MIN_DEAD = 0.5


@dataclass
class StorageStats:
    volumes: int = 0
    size: int = 0  # bytes on disk, every volume
    dead_bytes: int = 0  # bytes in sealed volumes that no stored structure covers
    report: Report = field(default_factory=Report)

    @property
    def healthy(self) -> bool:
        return self.report.ok

    def summary(self) -> str:
        """The panel's text: size, dead bytes and what the check found."""
        report = self.report
        text = f"{self.volumes} volume(s), {_megabytes(self.size)} on disk, {_megabytes(self.dead_bytes)} reclaimable"
        if not report.ok:
            return f"{text}\nUNHEALTHY: {len(report.broken)} unreadable structure(s), {len(report.missing_dicts)} missing dictionaries"
        leftovers = len(report.unreferenced) + len(report.empty) + len(report.orphans)
        return text + (f"\nHealthy; {leftovers} leftover item(s) a repair would clear" if leftovers else "\nHealthy")


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def storage_stats(store: PayloadStore, full: bool = False) -> StorageStats:
    """Size and dead bytes of the project's volumes, and the check report behind them (`full`
    reads every structure, not only the volume listing)."""
    report = store.check(full=full)
    with store.engine.connect() as conn:
        volumes = conn.exec_driver_sql("SELECT id, size FROM ms_volumes").fetchall()
    size = dead = 0
    for volume_id, recorded in volumes:
        try:
            size += store.path(volume_id).stat().st_size
        except FileNotFoundError:
            continue
        dead += int(report.dead.get(volume_id, 0.0) * max(recorded - HEADER, 0))
    return StorageStats(volumes=len(volumes), size=size, dead_bytes=dead, report=report)


def compact_storage(store: PayloadStore) -> dict:
    """Rewrite the volumes that are mostly dead (and merge small ones); what `compact` reports."""
    return store.compact()


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
        return "warning", "Project storage has leftovers", (
            f"{garbage} unused entries (structures no row points to any more) can be cleared: "
            "click the project name in the status bar, then Repair."
        )
    return "info", "Project storage checked", "Nothing to repair."


def repair_if_idle(project_db, *, busy: bool) -> Report:
    """Clear what `check` calls leftovers (unreferenced structures, empty volumes, orphan files); the
    new check. Never while a job runs: a structure it just wrote has no row pointing to it yet."""
    if busy:
        raise RuntimeError("Jobs are running; repair once they end.")
    return payload_store(project_db).repair()


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


__all__ = ["MIN_DEAD", "StorageStats", "check_storage", "compact_if_idle", "compact_storage", "repair_if_idle", "storage_stats"]
