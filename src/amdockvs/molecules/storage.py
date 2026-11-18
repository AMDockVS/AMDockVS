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
from ms_flow.core.data.catalog import activate_generation, active_generation_id, create_generation, shard_spec
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

    def promote(self, keys) -> dict[int, int]:
        """Library records, by `io.shards.shard_key`, into `molecules` rows: {key: molecule_id}.

        No gate: what a reduction keeps is already bounded by the reduction. Only the shards of
        the batches named are opened, and only the records asked for are loaded; a record that
        no longer parses is left out of the answer.
        """
        from ms_flow.core.data.shard import Shard

        from amdockvs.chemistry.descriptors import canonical_smiles
        from amdockvs.chemistry.shards import _molblock
        from amdockvs.core.payloads import structure_payload
        from amdockvs.io.batches import library_names
        from amdockvs.io.shards import FORMAT_FOR_KIND, mol_from_record, shard_ids, shard_key, split_shard_key
        from amdockvs.screening.materialize import promote_records

        wanted: dict[int, set[int]] = {}
        for key in keys:
            batch, record_id = split_shard_key(key)
            wanted.setdefault(batch, set()).add(record_id)
        libraries = library_names(self.project_db) if wanted else {}
        records: list[dict[str, Any]] = []
        for batch, record_ids in wanted.items():
            spec = shard_spec(scope=batch)
            for row in self.iter_rows(spec, batch_size=1000, whole=True):  # asked by key: excluded or not
                if not record_ids:
                    break
                with Shard.open(Path(str(row["path"])), ids=shard_ids(row)) as shard:
                    input_format = FORMAT_FOR_KIND.get(shard.kind, "")
                    for record_id in [value for value in record_ids if value in shard]:
                        record_ids.discard(record_id)
                        raw = shard[record_id].load()
                        mol = mol_from_record(bytes(raw).decode() if isinstance(raw, (bytes, bytearray)) else raw,
                                              input_format)
                        if mol is None:
                            continue
                        records.append({
                            "name": f"{libraries.get(batch, 'library')}:{record_id}",
                            "import_batch_id": batch,
                            "source_index": record_id,
                            "smiles": canonical_smiles(mol),
                            "structure": structure_payload(_molblock(mol), "sdf"),
                        })
        return {shard_key(batch, index): molecule_id
                for (batch, index), molecule_id in promote_records(self.project_db, records).items()}

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
    return db_count(project_db, shard_spec()) > 0


# Beside the input, in a folder named after the family: a campaign's stages read as a chain of folders.
PREPARED_DIR = "prepared_{engine}"


def prepared_shard_path(shard_path: str | Path, family: str, ids=None) -> Path:
    """Where a shard's prepared twin lives: derived, so `shard_preps` stores no path. `ids`: the
    shard's range inside its file (`shard_ids`), which names the twin of one of many."""
    from amdockvs.io.shards import shard_file_name

    shard_path = Path(shard_path)
    return shard_path.parent / PREPARED_DIR.format(engine=family) / shard_file_name(shard_path, ids)


def iter_prepared_shards(project_db, *, engine: str, unprepared: bool = False) -> Iterator[dict[str, Any]]:
    """Prepared shards minus the active exclusions (see `ShardStore.iter_rows`)."""
    return active_exclusions(project_db).narrow(_prepared_shards(project_db, engine=engine, unprepared=unprepared))


def _prepared_shards(project_db, *, engine: str, unprepared: bool = False) -> Iterator[dict[str, Any]]:
    """Prepared artifacts for one family, joined to their shard inventory.

    The in-memory map contains only shard metadata, never library molecules. A shard counts as
    prepared when it has a ``shard_preps`` row and its file is still there. With `unprepared`,
    the others come too, with an empty ``prepared_path`` and the count of the shard itself.
    """
    from sqlmodel import select

    from amdockvs.io.shards import shard_ids
    from ms_flow.core.database.project_models import CatalogShard

    from amdockvs.models import ShardPrep

    family = str(engine or "").strip().lower()
    generation_id = active_generation_id(project_db)
    with project_db.get_session() as session:
        counts = dict(session.exec(
            select(ShardPrep.shard_id, ShardPrep.n_records).where(ShardPrep.family == family)
        ).all())
        digests = dict(session.exec(
            select(ShardPrep.shard_id, ShardPrep.sha256).where(ShardPrep.family == family)
        ).all())
        # Scoped like every other shard read: the parent generation is still in the table and
        # its prepared artifacts are still on disk, but they are not the library any more.
        query = select(CatalogShard).order_by(CatalogShard.id)
        if generation_id is not None:
            query = query.where(CatalogShard.generation_id == generation_id)
        shards = list(session.exec(query).all())
    from amdockvs.io.batches import library_names

    libraries = library_names(project_db)
    for shard in shards:
        n_records = counts.get(int(shard.id or 0))
        ids = shard_ids(shard)
        prepared_path = prepared_shard_path(shard.path, family, ids)
        if n_records is not None and prepared_path.is_file():
            yield {
                "id": int(shard.id or 0),
                "scope": shard.scope,
                "library": libraries.get(shard.scope, "library"),
                "shard_index": int(shard.shard_index),
                "path": shard.path,
                "ids": ids,
                "base_id": shard.base_id,
                "slot_count": shard.slot_count,
                "prepared_path": str(prepared_path),
                "n_records": int(n_records),
                "sha256": digests.get(int(shard.id or 0)) or "",  # "" = prepared before 2026-11
            }
        elif unprepared:
            yield {"id": int(shard.id or 0), "scope": shard.scope,
                   "library": libraries.get(shard.scope, "library"), "shard_index": int(shard.shard_index),
                   "path": shard.path, "ids": ids, "base_id": shard.base_id, "slot_count": shard.slot_count,
                   "prepared_path": "", "n_records": int(shard.n_records),
                   "sha256": ""}


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
    "activate_generation",
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
    "prepared_shard_path",
    "prepared_shard_record_count",
    "shard_scope_spec",
    "store_from_config",
]
