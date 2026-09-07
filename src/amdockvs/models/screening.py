"""What an `htpvs` project knows about a library it never materializes.

Two tables, on purpose. A **shard** is inventory: thousands of rows, outlives the campaign,
answers *what do I have*. A **dispatch** is one source file handed to a cluster: dozens of
rows, dies with the campaign, answers *where did my work go*. Merging them would mean parent
rows with null `n_records` — null columns in the inventory to save a widget.

`screening_shards` starts empty and stays empty until something reports back: in an HPC
campaign the raw source travels and the *cluster* splits it, so the shard count is not known
when the work is dispatched. The dispatch row is what explains that emptiness.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from amdockvs.core.constants import (
    TABLE_SCREENING_DISPATCHES,
    TABLE_SCREENING_SHARD_RUNS,
    TABLE_SCREENING_SHARDS,
    TABLE_SCREENING_TARGETS,
    TABLE_SHARD_ENGINE_STATES,
)
from amdockvs.core.vocab import DispatchState, ShardState, TargetState


class ScreeningShard(SQLModel, table=True):
    __tablename__ = TABLE_SCREENING_SHARDS
    # A shard is addressed by (source, shard_index); the constraint is what keeps one import
    # from writing that pair twice.
    __table_args__ = (UniqueConstraint("source", "shard_index"),)

    id: int | None = Field(default=None, primary_key=True)
    # ponytail: the pair is unique *within* an import, not across imports. The parent-side
    # queue (io/shards.ShardQueueWriter) numbers shards as it cuts them, so a prefilter change
    # renumbers everything — re-importing the same file appends instead of replacing. A failed
    # import is restarted from scratch, not resumed; resuming would need span→shard receipts.
    source: str = Field(default="", index=True)
    shard_index: int = Field(default=0, index=True)
    path: str = Field(default="")
    input_format: str = Field(default="")
    n_records: int = Field(default=0)
    state: str = Field(default=ShardState.PENDING, index=True)
    error: str = Field(default="")
    dispatch_id: int | None = Field(default=None, foreign_key=f"{TABLE_SCREENING_DISPATCHES}.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ShardEngineState(SQLModel, table=True):
    """Reusable preparation artifacts for one shard and preparation family.

    The inventory path remains the current chemical shard. Docking-specific representations
    live here so AD4, DOCK6, DiffDock, and later families can coexist without rewriting the
    inventory row or treating a file extension as readiness state.
    """

    __tablename__ = TABLE_SHARD_ENGINE_STATES
    __table_args__ = (
        UniqueConstraint("shard_id", "engine"),
        Index("idx_shard_engine_ready", "engine", "is_ready"),
    )

    id: int | None = Field(default=None, primary_key=True)
    shard_id: int = Field(foreign_key=f"{TABLE_SCREENING_SHARDS}.id", index=True)
    engine: str = Field(default="", index=True)
    files: dict = Field(default_factory=dict, sa_type=JSON)
    n_records: int = Field(default=0)
    is_ready: bool = Field(default=False, index=True)
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
        cls,
        *,
        shard_id: int,
        engine: str,
        files: dict | None = None,
        n_records: int = 0,
        is_ready: bool = False,
        error: str = "",
    ) -> dict:
        now = datetime.now()
        return {
            "shard_id": int(shard_id),
            "engine": str(engine),
            "files": dict(files or {}),
            "n_records": int(n_records),
            "is_ready": bool(is_ready),
            "error": str(error or ""),
            "created_at": now,
            "updated_at": now,
        }


class ScreeningShardRun(SQLModel, table=True):
    """Idempotent receipt for one shard/receptor pair in a campaign.

    Target-level aggregate progress remains in ``ScreeningTarget``. The receptor key is needed
    so a partially completed campaign resumes only missing pairs instead of counting the same
    receptor twice after a retry.
    """

    __tablename__ = TABLE_SCREENING_SHARD_RUNS
    __table_args__ = (
        UniqueConstraint("run_id", "shard_id", "receptor_molecule_id", "engine", "protocol_hash"),
        Index("idx_screening_shard_run_lookup", "run_id", "engine", "protocol_hash", "state"),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(default="", index=True)
    shard_id: int = Field(foreign_key=f"{TABLE_SCREENING_SHARDS}.id", index=True)
    receptor_molecule_id: int = Field(default=0, index=True)
    engine: str = Field(default="", index=True)
    protocol_hash: str = Field(default="", index=True)
    ligands_scored: int = Field(default=0)
    hits: int = Field(default=0)
    result_path: str = Field(default="")
    state: str = Field(default=TargetState.SCHEDULED, index=True)
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ScreeningDispatch(SQLModel, table=True):
    __tablename__ = TABLE_SCREENING_DISPATCHES

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(default="", index=True)
    bytes: int = Field(default=0)
    state: str = Field(default=DispatchState.UPLOADED, index=True)
    # Filled from the cluster's feedback; the count is not knowable at dispatch time.
    n_shards_returned: int = Field(default=0)
    note: str = Field(default="")
    submitted_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ScreeningTarget(SQLModel, table=True):
    """One receptor of one campaign: what was scheduled, and how much of it has been docked.

    A campaign's receptors are *planned* before any of them produces a row — and in ranked mode
    nothing is written until it ends, so `docking_results` cannot answer "what is running and
    how far along". This is the only place that does. It is written by the parent: at submit
    (the plan) and once per returned chunk (the counters).
    """

    __tablename__ = TABLE_SCREENING_TARGETS
    __table_args__ = (UniqueConstraint("run_id", "receptor_molecule_id"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(default="", index=True)
    # ponytail: no foreign key on purpose. This is campaign bookkeeping, not a relation — a
    # receptor deleted mid-run must not fail the delete or take the run's history with it.
    receptor_molecule_id: int = Field(default=0, index=True)
    receptor_name: str = Field(default="")
    engine: str = Field(default="")
    ligands_total: int = Field(default=0)
    ligands_done: int = Field(default=0)
    hits: int = Field(default=0)
    state: str = Field(default=TargetState.SCHEDULED, index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


__all__ = [
    "ScreeningDispatch",
    "ScreeningShard",
    "ScreeningShardRun",
    "ScreeningTarget",
    "ShardEngineState",
]
