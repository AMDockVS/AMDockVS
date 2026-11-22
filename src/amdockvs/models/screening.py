"""What an `htpvs` project knows about a library it never materializes.

The shards themselves (path, id range, generation) are ms_flow's catalog
(`ms_flow.core.data.catalog`, table `ms_shard_files`: a row per file, a shard is a row group of
one); what is here is the domain around them.
Where a campaign's work went is the campaign's business (an ms_flow job in the journal), not a
table here: a campaign checksums the prepared files it sends itself (few big files, not one per
shard), so nothing here caches a digest a compaction would make stale.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_DOCKING_RUNS,
    TABLE_SCREENING_TARGETS,
)
from amdockvs.core.vocab import TargetState


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
    # The keys (`docking.results.dataset.state_key`, comma-joined: one per site) its shards were
    # docked under. What is done of each, and its result rows, are ms_flow's under that key.
    state_keys: str = Field(default="")


__all__ = [
    "ScreeningTarget",
]
