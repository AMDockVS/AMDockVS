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
and `library_kind()` reads it off the data instead of off a flag chosen before there was any
data. `io/api.py` is where it is enforced, at import.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from ms_flow.query import QuerySpec, db_count, db_pages

from amdockvs.core.constants import TABLE_MOLECULES, TABLE_SCREENING_SHARDS
from amdockvs.screening.materialize import HitGate, PAYLOAD_LIGHT, ingest_hits
from amdockvs.core.vocab import MoleculeType, MoleculeUsageClass, ProjectMode


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


SHARD_FIELDS = (
    "id", "generation_id", "import_batch_id", "shard_index", "path", "n_records"
)


def active_generation_id(project_db) -> int | None:
    """Which rewrite of the library is the current one. `None` before the first import."""
    from sqlmodel import select

    from amdockvs.models import ShardGeneration

    with project_db.get_session() as session:
        row = session.exec(
            select(ShardGeneration)
            .where(ShardGeneration.is_active == True)  # noqa: E712 - SQL, not Python truthiness
            .order_by(ShardGeneration.id.desc())
        ).first()
        return int(row.id) if row is not None else None


def create_generation(project_db, *, step: str, shard_dir: str | Path, job_id: str = "") -> int:
    """Open a new generation, parented to whatever is active. It is not active yet.

    Nothing reads it until `activate_generation` flips the pointer, which is what makes a
    failed rewrite a no-op instead of a half-switched inventory.
    """
    from amdockvs.models import ShardGeneration

    parent_id = active_generation_id(project_db)
    with project_db.get_session() as session:
        row = ShardGeneration(
            parent_id=parent_id,
            step=str(step or ""),
            shard_dir=str(shard_dir or ""),
            job_id=str(job_id or ""),
            is_active=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


def activate_generation(project_db, generation_id: int) -> None:
    """Make one generation the library, and forget its grandparent.

    The switch itself is one UPDATE over a handful of generation rows — the inventory's
    thousands of shard rows are never touched. The sweep afterwards is the window of two:
    the active generation and its parent survive, everything older goes, files included.
    """
    from sqlmodel import select

    from amdockvs.models import ScreeningShard, ShardGeneration

    with project_db.get_session() as session:
        generations = {int(row.id): row for row in session.exec(select(ShardGeneration)).all()}
        target = generations.get(int(generation_id))
        if target is None:
            raise ValueError(f"No shard generation {generation_id} to activate.")
        for row in generations.values():
            row.is_active = row.id == target.id
            session.add(row)
        keep = {int(target.id)}
        if target.parent_id is not None:
            keep.add(int(target.parent_id))
        doomed = [gen_id for gen_id in generations if gen_id not in keep]
        paths = [
            str(shard.path or "")
            for shard in session.exec(
                select(ScreeningShard).where(ScreeningShard.generation_id.in_(doomed))
            ).all()
        ] if doomed else []
        for gen_id in keep:
            # The surviving parent points at a generation that is about to stop existing. Cut
            # the link first or the delete trips the foreign key — its lineage ends here now.
            generation = generations[gen_id]
            if generation.parent_id is not None and int(generation.parent_id) in doomed:
                generation.parent_id = None
                session.add(generation)
        session.flush()
        for gen_id in doomed:
            for shard in session.exec(
                select(ScreeningShard).where(ScreeningShard.generation_id == gen_id)
            ).all():
                session.delete(shard)
            session.delete(generations[gen_id])
        session.commit()
    # Files last: a row that survives a missing file is a bug you can see, a file that
    # survives its row is an orphan nothing will ever look at again.
    for text in paths:
        path = Path(text)
        if path.is_file():
            path.unlink(missing_ok=True)


def shard_scope_spec() -> QuerySpec:
    """Every shard; `ShardStore` pins it to the active generation."""
    return QuerySpec(table=TABLE_SCREENING_SHARDS, fields=SHARD_FIELDS, order=("id",))


class ShardStore:
    """`htpvs`: the ligands are records inside shard files; the db only indexes the shards."""

    def __init__(self, project_db, *, gate: HitGate | None = None, payload: str = PAYLOAD_LIGHT):
        self.project_db = project_db
        # No gate, no promotion: in `htpvs` the cap is not an option, it is what keeps the
        # project database from filling with the library it exists to keep out.
        self.gate = gate
        self.payload = payload

    def _scoped(self, spec: QuerySpec | None) -> QuerySpec:
        """Pin any shard scope to the active generation.

        Injected here rather than in `shard_scope_spec` because this is the only place that
        has the database — and because a caller that forgets it would silently feed a step
        two generations of the same library at once. A spec that names a generation is left
        alone: that is how a redo reads its parent.
        """
        spec = spec or shard_scope_spec()
        if "generation_id" in spec.filters:
            return spec
        generation_id = active_generation_id(self.project_db)
        if generation_id is None:
            return spec
        return replace(spec, filters={**spec.filters, "generation_id": generation_id})

    def iter_rows(self, spec: QuerySpec | None = None, *, batch_size: int = 1) -> Iterator[dict[str, Any]]:
        """Shards, one row each. `batch_size` is a paging detail — a shard is the work unit."""
        yield from db_pages(self.project_db, self._scoped(spec), page_size=max(1, int(batch_size)))

    def count(self, spec: QuerySpec | None = None) -> int:
        """How many shards are in scope — this is what a run declares as its work."""
        return db_count(self.project_db, self._scoped(spec))

    def record_count(self, spec: QuerySpec | None = None) -> int:
        """How many molecules those shards hold, without opening a single file.

        ponytail: summed over the inventory rows rather than in SQL. The inventory is
        thousands of rows at worst — it is the *library* that must never be walked, and it
        isn't. A SUM query if the shard count ever reaches millions.
        """
        return sum(int(row.get("n_records") or 0) for row in self.iter_rows(spec, batch_size=1000))

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
        from amdockvs.io.shards import FORMAT_FOR_KIND, mol_from_record, shard_key, split_shard_key
        from amdockvs.screening.materialize import promote_records

        wanted: dict[int, set[int]] = {}
        for key in keys:
            batch, record_id = split_shard_key(key)
            wanted.setdefault(batch, set()).add(record_id)
        libraries = library_names(self.project_db) if wanted else {}
        records: list[dict[str, Any]] = []
        for batch, record_ids in wanted.items():
            spec = replace(shard_scope_spec(), filters={"import_batch_id": batch})
            for row in self.iter_rows(spec, batch_size=1000):
                if not record_ids:
                    break
                with Shard.open(Path(str(row["path"]))) as shard:
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
    return db_count(project_db, shard_scope_spec()) > 0


def library_kind(project_db) -> str:
    """Where this project's screening library lives — derived from the data, never declared.

    A project is not "an htpvs project" because someone ticked a box before importing
    anything; it is one because it holds shards. The badge, and anything else that wants to
    know, asks here.
    """
    return ProjectMode.HTPVS if has_shards(project_db) else ProjectMode.VS


# Beside the input, in a folder named after the family: a campaign's stages read as a chain of folders.
PREPARED_DIR = "prepared_{engine}"


def prepared_shard_path(shard_path: str | Path, family: str) -> Path:
    """Where a shard's prepared twin lives: derived, so `shard_preps` stores no path."""
    shard_path = Path(shard_path)
    return shard_path.parent / PREPARED_DIR.format(engine=family) / shard_path.name


def iter_prepared_shards(project_db, *, engine: str) -> Iterator[dict[str, Any]]:
    """Prepared artifacts for one family, joined to their shard inventory.

    The in-memory map contains only shard metadata, never library molecules. A shard counts as
    prepared when it has a ``shard_preps`` row and its file is still there.
    """
    from sqlmodel import select

    from amdockvs.models import ScreeningShard, ShardPrep

    family = str(engine or "").strip().lower()
    generation_id = active_generation_id(project_db)
    with project_db.get_session() as session:
        counts = dict(session.exec(
            select(ShardPrep.shard_id, ShardPrep.n_records).where(ShardPrep.family == family)
        ).all())
        # Scoped like every other shard read: the parent generation is still in the table and
        # its prepared artifacts are still on disk, but they are not the library any more.
        query = select(ScreeningShard).order_by(ScreeningShard.id)
        if generation_id is not None:
            query = query.where(ScreeningShard.generation_id == generation_id)
        shards = list(session.exec(query).all())
    from amdockvs.io.batches import library_names

    libraries = library_names(project_db)
    for shard in shards:
        n_records = counts.get(int(shard.id or 0))
        prepared_path = prepared_shard_path(shard.path, family)
        if n_records is not None and prepared_path.is_file():
            yield {
                "id": int(shard.id or 0),
                "import_batch_id": shard.import_batch_id,
                "library": libraries.get(shard.import_batch_id, "library"),
                "shard_index": int(shard.shard_index),
                "path": shard.path,
                "prepared_path": str(prepared_path),
                "n_records": int(n_records),
            }


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
    return db_count(
        project_db,
        QuerySpec(table=TABLE_MOLECULES, fields=("id",),
                  filters={"is_ligand": True, "excluded": False}, limit=1),
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
    "has_ligand_rows",
    "library_kind",
    "PREPARED_DIR",
    "iter_prepared_shards",
    "prepared_shard_path",
    "prepared_shard_record_count",
    "shard_scope_spec",
    "store_from_config",
]
