"""What an `htpvs` project knows about a library it never materializes.

The shards themselves (path, id range, generation) are ms_flow's catalog
(`ms_flow.core.data.catalog`, table `ms_shards`); what is here is the domain around them.
Where a campaign's work went is the campaign's business (an ms_flow job in the journal), not a
table here: only prepared shards travel to a cluster, by the sha256 their `shard_preps` row keeps.
"""

from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_DOCKING_RUNS,
    TABLE_SCREENING_SHARD_RUNS,
    TABLE_SCREENING_TARGETS,
    TABLE_SHARD_PREPS,
)
from amdockvs.core.vocab import TargetState


class ShardPrep(SQLModel, table=True):
    """A shard prepared for one family: the row exists = prepared. Its file is derived
    (`molecules.storage.prepared_shard_path`), not stored; failures are counted, not listed."""

    __tablename__ = TABLE_SHARD_PREPS
    __table_args__ = ({"sqlite_with_rowid": False},)

    shard_id: int = Field(foreign_key="ms_shards.id", primary_key=True)  # ms_flow's catalog
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
    shard_id: int = Field(foreign_key="ms_shards.id", index=True)
    receptor_molecule_id: int = Field(default=0, index=True)
    engine: str = code_field("engine", index=True)
    protocol_hash: str = Field(default="", index=True)
    ligands_scored: int = Field(default=0)
    hits: int = Field(default=0)
    result_path: str = Field(default="")
    # Which ligands of the shard this receipt stands for: empty is the whole shard, and
    # ``top:<reference receptor id>`` is an off-target stage, which docks only the Top-N
    # picked from that receptor. A forced re-dock replaces a receipt only when what it
    # docked again covers it, so a subset never retires a whole-shard receipt.
    selection: str = Field(default="")
    # What the shard was docked against (`docking_input_key`): a later skip_existing run skips
    # the pair only while the receptor preparation and box are still the same.
    input_key: str = Field(default="")
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


__all__ = [
    "ScreeningShardRun",
    "ScreeningTarget",
    "ShardPrep",
]
