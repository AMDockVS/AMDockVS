"""Where the ligands of a project live.

Every difference between `vs` and `htpvs` is the same question asked at a different stage:
import asks *where do I write*, preprocessing/diversity/preparation/docking ask *where do I
read*, analysis asks *when do I write*. Six bullet points, one seam. Receptors are rows in
both modes, so the seam is ligands only.

`DbStore` is `vs`: the ligands are rows in the project database and the store is exactly the
`db_pages` / `db_count` pair that every feed already called by hand. `ShardStore` is `htpvs`:
the rows are shards, not molecules, and one shard is one unit of work.

The two are **not** interchangeable behind the same tool. A `DbStore` feed is asked for
molecule rows by a molecule `QuerySpec`; a `ShardStore` feed answers with shards, and its
consumer runs `transform_ligand_shard`, not `transform_ligand_rows`. A tool declares which one
it consumes — that is the type gate, and it is why nothing here dispatches on a project flag.

**The invariant**: one screening library per project, in one store. Curated molecules
(receptors, reference ligands, a QSAR training set) are always rows; the screening library is
either rows or shards, never both. That is what keeps a scope from ever having to span the two,
and `has_shards()` reads it off the data instead of off a flag chosen before there was any
data. `io/api.py` is where it is enforced, at import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from ms_flow.core.data import catalog
from ms_flow.core.data.catalog import active_generation_id, create_generation, shard_spec
from ms_flow.core.data.exclusions import active_exclusions, exclude
from ms_flow.query import QuerySpec, db_count, db_pages

from amdockvs.core.constants import TABLE_MOLECULES
from amdockvs.screening.materialize import HitGate, PAYLOAD_LIGHT, ingest_hits
from amdockvs.core.vocab import MoleculeType, MoleculeUsageClass


class LigandStore(Protocol):
    """What a feed needs from the ligand library, whatever holds it."""

    def iter_rows(self, spec: QuerySpec, *, batch_size: int = 128) -> Iterator[dict[str, Any]]:
        """The rows in `spec`, streamed. Never materializes the library."""

    def count(self, spec: QuerySpec) -> int:
        """How many rows `spec` matches, without walking them."""

    def materialize(self, rows) -> list[dict[str, Any]]:
        """Turn results into project rows. A no-op in `vs`, a promotion in `htpvs`."""


class DbStore:
    """`vs`: the ligands are rows in the project database."""

    def __init__(self, project_db):
        self.project_db = project_db

    def iter_rows(self, spec: QuerySpec, *, batch_size: int = 128) -> Iterator[dict[str, Any]]:
        yield from db_pages(self.project_db, spec, page_size=max(1, int(batch_size)))

    def count(self, spec: QuerySpec) -> int:
        return db_count(self.project_db, spec)

    def materialize(self, rows) -> list[dict[str, Any]]:
        """Already materialized. This no-op is what lets the analysis code be the same in
        both modes instead of branching on where the hit came from."""
        return [dict(row) for row in rows]


def shard_generation_progress(project_db, generation_id: int) -> tuple[int, int]:
    """(ligands written into `generation_id`, ligands of the library it rewrites).

    While the step runs the active generation is still its parent; once it switches, both are
    the new one, so a finished step reads done == total.
    """
    return (catalog.record_count(project_db, shard_spec(generation_id=int(generation_id)), whole=True),
            catalog.record_count(project_db, whole=True))


def shard_scope_spec() -> QuerySpec:
    """Every shard; reads pin it to the active generation (`ms_flow.core.data.catalog`)."""
    return shard_spec()


class ShardStore:
    """`htpvs`: the ligands are records inside shard files; the db only indexes the shards."""

    def __init__(self, project_db, *, gate: HitGate | None = None, payload: str = PAYLOAD_LIGHT):
        self.project_db = project_db
        # No gate, no promotion: in `htpvs` the cap is not an option, it is what keeps the
        # project database from filling with the library it exists to keep out.
        self.gate = gate
        self.payload = payload

    def iter_rows(
        self, spec: QuerySpec | None = None, *, batch_size: int = 1, whole: bool = False
    ) -> Iterator[dict[str, Any]]:
        """Shards, one row each, as ms_flow's catalog gives them: on the active generation and
        minus the active exclusions (a partial shard carries `record_ids`). `whole`: every
        record, for the steps that transform rather than select (preparation, chemistry)."""
        return catalog.shards(self.project_db, spec, whole=whole, page_size=batch_size)

    def count(self, spec: QuerySpec | None = None, *, whole: bool = False) -> int:
        """How many shards `iter_rows` yields — this is what a run declares as its work."""
        return catalog.count(self.project_db, spec, whole=whole)

    def exclude(self, keys, *, reason: str = "", producer_job_id: str = "") -> int | None:
        """Leave records out by `io.shards.shard_key`, without touching a shard: the cut is a
        sidecar ms_flow keeps. Its artifact id (to undo it), None when nothing was cut."""
        from amdockvs.io.shards import split_shard_key

        by_batch: dict[int, list[int]] = {}
        for key in keys:
            batch, record_id = split_shard_key(key)
            by_batch.setdefault(int(batch), []).append(int(record_id))
        return exclude(self.project_db, by_batch, reason=reason, producer_job_id=producer_job_id)

    def record_count(self, spec: QuerySpec | None = None, *, whole: bool = False) -> int:
        """How many molecules those shards hold, without opening a single file."""
        return catalog.record_count(self.project_db, spec, whole=whole)

    def materialize(self, rows) -> list[dict[str, Any]]:
        """Promote hits into `molecules` + `docking_results`, threshold and cap enforced.

        Incremental: call it per batch of returned hits. The gate lives on the store, so the
        cap holds across calls for the whole campaign.
        """
        if self.gate is None:
            raise ValueError(
                "ShardStore.materialize needs a HitGate: promoting hits without a cap is the "
                "failure mode this exists to prevent. ShardStore(db, gate=HitGate(cap=...))."
            )
        return ingest_hits(
            self.project_db,
            rows,
            gate=self.gate,
            payload=self.payload,
        )

    def finalize(self) -> list[dict[str, Any]]:
        """Write whatever a ranked gate held back until the end (nothing, for a streaming one).

        The drained rows already went through the gate, so the one here only sizes the write.
        """
        rows = [] if self.gate is None else self.gate.drain()
        if not rows:
            return []
        return ingest_hits(
            self.project_db,
            rows,
            gate=HitGate(cap=len(rows)),
            payload=self.payload,
        )




def as_store(source) -> LigandStore:
    """Accept a store or a bare project db; a project db means `vs`.

    Feeds are called both from a job (which has a config, so a real store) and from the UI's
    inline previews (which have only `project_db`). Without this every one of those call sites
    would need the store threaded through it to keep doing exactly what it already does.
    """
    return source if hasattr(source, "iter_rows") else DbStore(source)


def has_shards(project_db) -> bool:
    """Does this project hold a sharded screening library?"""
    return catalog.has_files(project_db)


# Beside the input, in a folder named after the family: a campaign's stages read as a chain of folders.
PREPARED_DIR = "prepared_{engine}"


def prepared_role(family: str) -> str:
    """The catalog role of a family's prepared files (ms_flow derived files: same ids, ``origin``
    = the source shard). The catalog row is the state: no table of its own."""
    return f"prepared:{str(family or '').strip().lower()}"


def prepared_dir(shard_path: str | Path, family: str) -> Path:
    return Path(shard_path).parent / PREPARED_DIR.format(engine=str(family or "").strip().lower())


def iter_prepared_shards(project_db, *, engine: str, unprepared: bool = False) -> Iterator[dict[str, Any]]:
    """Prepared shards minus the active exclusions (see `ShardStore.iter_rows`)."""
    return active_exclusions(project_db).narrow(_prepared_shards(project_db, engine=engine, unprepared=unprepared))


def _prepared_shards(project_db, *, engine: str, unprepared: bool = False) -> Iterator[dict[str, Any]]:
    """The library's shards, each with its newest prepared copy of one family: ``prepared_path``
    and ``prepared_shard`` (its number in that file). With `unprepared`, the others come too, with
    an empty ``prepared_path`` and the count of the shard itself.
    ponytail: one dict entry per prepared shard (4k per 1M records)."""
    from amdockvs.io.batches import library_names
    from amdockvs.io.shards import shard_number

    made = {int(row["origin"]): row for row in catalog.derived(project_db, prepared_role(engine), whole=True)}
    libraries = library_names(project_db)
    # Scoped like every other shard read: the parent generation is still in the table, but it is
    # not the library any more.
    for shard in catalog.shards(project_db, whole=True):
        row = {
            "id": int(shard["id"]),
            "file_id": int(shard["file_id"]),
            "generation_id": shard["generation_id"],
            "scope": shard["scope"],
            "library": libraries.get(shard["scope"], "library"),
            "shard_index": int(shard["shard_index"]),
            "path": shard["path"],
            "shard": shard_number(shard),
            "base_id": shard["base_id"],
            "slot_count": shard["slot_count"],
        }
        twin = made.get(int(shard["id"]))
        if twin is not None:
            # the prepared copy has the source's range; its own bitmap says which ids it could make.
            # ponytail: past ms_flow PRESENT_SLOTS (65536; shards hold 256) narrow would read the
            # library shard's ids instead; pass the prepared path then.
            yield {**row, "prepared_path": str(twin["path"]), "prepared_shard": shard_number(twin),
                   "n_records": int(twin["n_records"]), "n_failed": int(twin["n_dropped"]),
                   "present": twin.get("present")}
        elif unprepared:
            yield {**row, "prepared_path": "", "prepared_shard": None, "n_records": int(shard["n_records"]),
                   "present": shard.get("present")}


def _prepared_roles(project_db) -> list[str]:
    from sqlmodel import select

    from ms_flow.core.database.project_models import CatalogFile

    with project_db.get_session() as session:
        return list(session.exec(select(CatalogFile.role).where(CatalogFile.role.like("prepared:%")).distinct()).all())


def prepared_counts(project_db, spec: QuerySpec | None = None) -> dict[str, tuple[int, int, int]]:
    """family -> (prepared shards, prepared records, failed records) over the library shards of
    `spec` (None = the active library), each read from its newest prepared copy."""
    counts = {}
    for role in _prepared_roles(project_db):
        made = list(catalog.derived(project_db, role, spec, whole=True))
        if made:
            counts[role.split(":", 1)[1]] = (len(made), sum(int(r["n_records"]) for r in made),
                                             sum(int(r["n_dropped"]) for r in made))
    return counts


def prepared_failures(project_db, spec: QuerySpec | None = None) -> dict[str, int]:
    """reason -> how many records of the library shards of `spec` a preparation dropped (the
    dropped rows of their newest prepared copies)."""
    from collections import Counter

    from ms_flow.core.data.shard import read_dropped

    from amdockvs.io.shards import shard_number

    tally: Counter = Counter()
    for role in _prepared_roles(project_db):
        for row in catalog.derived(project_db, role, spec, whole=True):
            if row["n_dropped"]:
                tally.update(reason for _id, reason in read_dropped(row["path"], shard_number(row)))
    return dict(tally)


def prepared_shard_record_count(project_db, *, engine: str) -> int:
    """Molecules in readable prepared artifacts, without opening a shard."""
    return sum(int(row.get("n_records") or 0) for row in iter_prepared_shards(project_db, engine=engine))


# The screening library: general-purpose small molecules in ligand role. Receptors, reference
# ligands (cocrystals) and QSAR training sets are *not* this — they are curated rows and they
# coexist with a sharded library without ambiguity.
GENERAL_LIGAND_FILTERS = {
    "is_ligand": True,
    "usage_class": MoleculeUsageClass.GENERAL,
    "molecule_type": MoleculeType.SMALL_MOLECULE,
}

LIBRARY_ROWS = "rows"
LIBRARY_SHARDS = "shards"


def general_ligand_count(project_db) -> int:
    """How many screening ligands are rows in this project."""
    return db_count(
        project_db,
        QuerySpec(table=TABLE_MOLECULES, fields=("id",), filters=dict(GENERAL_LIGAND_FILTERS)),
    )


def has_ligand_rows(project_db) -> bool:
    """Is any ligand a row here — screening, reference or promoted hit?

    Wider than `general_ligand_count` on purpose: this answers "is the Ligands table worth
    opening", and a project whose only ligands are reference cocrystals still is. One row
    answers it: counting them all is a scan of millions.
    """
    return _has_row(project_db, "is_ligand")


def has_receptor_rows(project_db) -> bool:
    """Is any receptor a row here? One row answers it, like `has_ligand_rows`."""
    return _has_row(project_db, "is_receptor")


def _has_row(project_db, role: str) -> bool:
    return db_count(
        project_db,
        QuerySpec(table=TABLE_MOLECULES, fields=("id",), filters={role: True, "excluded": False}, limit=1),
    ) > 0


def store_from_config(config: Mapping[str, Any] | None) -> LigandStore:
    """A chunk feed gets a `config`, not the runtime — this is where it resolves its store.

    Always rows: these are the row tools (chemistry over molecules, descriptors, diversity).
    The shard tools do not go through here — they build a `ShardStore` explicitly, because
    consuming shards is a different feed, not the same feed against another backend.
    """
    return as_store(dict(config or {}).get("project_db"))


__all__ = [
    "DbStore",
    "active_generation_id",
    "create_generation",
    "LigandStore",
    "ShardStore",
    "GENERAL_LIGAND_FILTERS",
    "LIBRARY_ROWS",
    "LIBRARY_SHARDS",
    "as_store",
    "general_ligand_count",
    "has_shards",
    "shard_generation_progress",
    "has_ligand_rows",
    "has_receptor_rows",
    "PREPARED_DIR",
    "iter_prepared_shards",
    "prepared_counts",
    "prepared_failures",
    "prepared_dir",
    "prepared_role",
    "prepared_shard_record_count",
    "shard_scope_spec",
    "store_from_config",
]
