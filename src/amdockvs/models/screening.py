"""What an `htpvs` project knows about a library it never materializes.

A **shard** is inventory: thousands of rows, outlives every campaign, answers *what do I have*.
Where a campaign's work went is the campaign's business (an ms_flow job in the journal), not a
table here: only prepared shards travel to a cluster, by the sha256 their `shard_preps` row keeps.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, JSON, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_DOCKING_RUNS,
    TABLE_IMPORT_BATCHES,
    TABLE_SCREENING_SHARD_RUNS,
    TABLE_SCREENING_SHARDS,
    TABLE_SCREENING_TARGETS,
    TABLE_SHARD_PREPS,
    TABLE_SHARD_GENERATIONS,
)
from amdockvs.core.vocab import TargetState


class ShardGeneration(SQLModel, table=True):
    """One rewrite of the whole sharded library, and what it came from.

    Every step that writes shards must renumber: a shard's id span is capped by the
    container's uint16 slot map, so survivors of a filter cannot keep the ids they had.
    Renumbering without lineage would lose provenance, so a rewrite produces a *generation*
    instead of overwriting the inventory in place.

    Only one generation is active. The switch is a single UPDATE of this flag at the end of a
    job — never a row-by-row replacement, which is what left the inventory half-switched when
    a job died mid-flight.

    **Window of two**: activating a generation forgets its grandparent, rows and files. In
    `htpvs` the shards *are* the memory — there is no database row behind a library molecule
    to fall back on — but keeping every generation would mean keeping the whole library once
    per step. Two is what a redo needs: the current state and the one it came from.
    """

    __tablename__ = TABLE_SHARD_GENERATIONS

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int | None = Field(default=None, foreign_key=f"{TABLE_SHARD_GENERATIONS}.id", index=True)
    # What produced it: "import", or the pipeline label of the step that rewrote the shards.
    step: str = Field(default="")
    shard_dir: str = Field(default="")
    job_id: str = Field(default="")
    is_active: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


class ScreeningShard(SQLModel, table=True):
    __tablename__ = TABLE_SCREENING_SHARDS
    # A shard is addressed by (generation, import batch, shard_index). Without the generation
    # the pair collides the moment a step rewrites the library.
    __table_args__ = (UniqueConstraint("generation_id", "import_batch_id", "shard_index"),)

    id: int | None = Field(default=None, primary_key=True)
    generation_id: int | None = Field(
        default=None, foreign_key=f"{TABLE_SHARD_GENERATIONS}.id", index=True
    )
    # ponytail: the index is unique *within* a batch. The parent-side queue
    # (io/shards.ShardQueueWriter) numbers shards as it cuts them, and every import is a new
    # batch, so re-importing the same file appends instead of replacing. A failed import is
    # restarted from scratch, not resumed; resuming would need span→shard receipts.
    import_batch_id: int | None = Field(default=None, foreign_key=f"{TABLE_IMPORT_BATCHES}.id", index=True)
    shard_index: int = Field(default=0, index=True)
    path: str = Field(default="")  # the .mshard's payload kind says the format
    n_records: int = Field(default=0)
    error: str = Field(default="")


class ShardPrep(SQLModel, table=True):
    """A shard prepared for one family: the row exists = prepared. Its file is derived
    (`molecules.storage.prepared_shard_path`), not stored; failures are counted, not listed."""

    __tablename__ = TABLE_SHARD_PREPS
    __table_args__ = ({"sqlite_with_rowid": False},)

    shard_id: int = Field(foreign_key=f"{TABLE_SCREENING_SHARDS}.id", primary_key=True)
    family: str = code_field("prep_family", "ad4", primary_key=True)
    n_records: int = Field(default=0)
    n_failed: int = Field(default=0)  # > 0 = partial
    sha256: str = Field(default="")  # of the prepared file: a campaign sends it without reading it again


class ScreeningShardRun(SQLModel, table=True):
    """Idempotent receipt for one shard/receptor pair in a campaign.

    Target-level aggregate progress remains in ``ScreeningTarget``. The receptor key is needed
    so a partially completed campaign resumes only missing pairs instead of counting the same
    receptor twice after a retry.
    """

    __tablename__ = TABLE_SCREENING_SHARD_RUNS
    __table_args__ = (
        UniqueConstraint("docking_run_id", "shard_id", "receptor_molecule_id"),
        Index("idx_screening_shard_run_lookup", "docking_run_id", "state"),
    )

    id: int | None = Field(default=None, primary_key=True)
    docking_run_id: int = Field(foreign_key=f"{TABLE_DOCKING_RUNS}.id")
    shard_id: int = Field(foreign_key=f"{TABLE_SCREENING_SHARDS}.id", index=True)
    receptor_molecule_id: int = Field(default=0, index=True)
    engine: str = code_field("engine", index=True)
    protocol_hash: str = Field(default="", index=True)
    ligands_scored: int = Field(default=0)
    hits: int = Field(default=0)
    result_path: str = Field(default="")
    state: str = code_field("run_state", TargetState.SCHEDULED, index=True)
    error: str = Field(default="")


class ScreeningTarget(SQLModel, table=True):
    """One receptor of one campaign: what was scheduled, and how much of it has been docked.

    A campaign's receptors are *planned* before any of them produces a row — and in ranked mode
    nothing is written until it ends, so `docking_results` cannot answer "what is running and
    how far along". This is the only place that does. It is written by the parent: at submit
    (the plan) and once per returned chunk (the counters).
    """

    __tablename__ = TABLE_SCREENING_TARGETS
    __table_args__ = (UniqueConstraint("docking_run_id", "receptor_molecule_id"),)

    id: int | None = Field(default=None, primary_key=True)
    docking_run_id: int = Field(foreign_key=f"{TABLE_DOCKING_RUNS}.id")
    # ponytail: no foreign key on purpose. This is campaign bookkeeping, not a relation — a
    # receptor deleted mid-run must not fail the delete or take the run's history with it.
    receptor_molecule_id: int = Field(default=0, index=True)
    receptor_name: str = Field(default="")
    engine: str = code_field("engine")
    ligands_total: int = Field(default=0)
    ligands_done: int = Field(default=0)
    hits: int = Field(default=0)  # passed the prefilter
    imported: int = Field(default=0)  # of those, materialized as rows (the top-N)
    state: str = code_field("run_state", TargetState.SCHEDULED, index=True)
    # A forced re-dock docks again what was docked: its Docked counts its own results only.
    forced: bool = Field(default=False)


__all__ = [
    "ScreeningShard",
    "ScreeningShardRun",
    "ScreeningTarget",
    "ShardPrep",
    "ShardGeneration",
]
