"""What a project's structure volumes hold, for the storage panel: size on disk, dead bytes
(structures that were deleted and not yet compacted away), a health check and a compaction.

Blocking and Qt-free: the UI runs them with `run_async`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ms_flow.payloads import HEADER, PayloadStore, Report


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


__all__ = ["StorageStats", "compact_storage", "storage_stats"]
