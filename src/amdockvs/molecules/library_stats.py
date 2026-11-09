"""What a sharded screening library holds, for the Project summary.

In a sharded project the library is not in `molecules`, so the row counts read zero. These are
read off the shard inventory instead — never off the shard files, which are only stat'ed for
their size.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LibraryStats:
    ligands: int = 0  # records in the active generation
    shards: int = 0
    failed_shards: int = 0  # shards a step could not write: their ligands are missing
    last_step: str = ""  # what produced the active generation ("import", "protonate+generate_3d")
    previous_step: str = ""  # the generation kept for a redo, when there is one
    # engine family -> (shards prepared, ligands prepared, ligands that failed preparation)
    prepared: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    campaigns: int = 0
    receptors_docked: int = 0
    ligands_docked: int = 0  # ligand x receptor pairs scored, over every campaign
    hits: int = 0
    disk_bytes: int = 0  # the whole shards folder: both generations, prepared twins, results


def _folder_bytes(root: Path) -> int:
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def library_stats(project_db, shards_root: str | Path | None = None) -> LibraryStats:
    """Read the inventory of the active generation and what was done with it."""
    from sqlmodel import func, select

    from amdockvs.models import ScreeningShard, ScreeningTarget, ShardGeneration, ShardPrep
    from amdockvs.molecules.storage import active_generation_id

    generation_id = active_generation_id(project_db)
    if generation_id is None:
        return LibraryStats()
    in_library = ScreeningShard.generation_id == generation_id
    with project_db.get_session() as session:
        shards, ligands = session.exec(
            select(func.count(ScreeningShard.id), func.coalesce(func.sum(ScreeningShard.n_records), 0)).where(in_library)
        ).one()
        failed = session.exec(
            select(func.count(ScreeningShard.id)).where(in_library, ScreeningShard.error != "")
        ).one()
        generation = session.get(ShardGeneration, generation_id)
        parent = session.get(ShardGeneration, generation.parent_id) if generation and generation.parent_id else None
        prepared = {
            str(family): (int(n_shards), int(n_records or 0), int(n_failed or 0))
            for family, n_shards, n_records, n_failed in session.exec(
                select(ShardPrep.family, func.count(ShardPrep.shard_id), func.sum(ShardPrep.n_records),
                       func.sum(ShardPrep.n_failed))
                .join(ScreeningShard, ScreeningShard.id == ShardPrep.shard_id)
                .where(in_library)
                .group_by(ShardPrep.family)
            ).all()
        }
        campaigns, receptors, docked, hits = session.exec(
            select(func.count(func.distinct(ScreeningTarget.docking_run_id)),
                   func.count(func.distinct(ScreeningTarget.receptor_molecule_id)),
                   func.coalesce(func.sum(ScreeningTarget.ligands_done), 0),
                   func.coalesce(func.sum(ScreeningTarget.hits), 0))
        ).one()
    root = Path(shards_root) if shards_root else None
    return LibraryStats(
        ligands=int(ligands),
        shards=int(shards),
        failed_shards=int(failed),
        last_step=str(generation.step or "") if generation else "",
        previous_step=str(parent.step or "") if parent else "",
        prepared=prepared,
        campaigns=int(campaigns),
        receptors_docked=int(receptors),
        ligands_docked=int(docked),
        hits=int(hits),
        disk_bytes=_folder_bytes(root) if root is not None and root.is_dir() else 0,
    )


def format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


__all__ = ["LibraryStats", "format_bytes", "library_stats"]
