"""Split a raw library into shards, without materializing a single molecule row.

An `htpvs` import is the normal import minus the expensive half: the feed is the same cheap
byte-level record splitter (`loaders.stream_import_payload_batches`), but the records end up
in shard containers instead of being parsed into rows of the project database.

The work is split in two. Workers **filter**: they parse their span only where a prefilter
demands it, and hand back the survivors. A single parent-side queue (`ShardQueueWriter`)
**cuts**: it packs exactly `shard_size` survivors per shard, so shards are uniform whatever
the filter rejected, and ids run correlative across the whole import.

A shard is not a file in the source's format: it is a container (`ms_flow.core.data.shard`)
holding the raw record bytes, with the format in its header. Nothing downstream sniffs an
extension. The ordinal a record had in its source file does not survive as its id — it lives
in the shard's `source_indices` metadata, which is where provenance goes once the queue
decides who sits next to whom.
"""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ms_flow.core.data.shard import PayloadKind, Serializer, ShardWriter

from amdockvs.io.parsers import iter_record_span
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.rows import cull_mol, htp_mol_filter
from amdockvs.core.vocab import FileFormat, ShardState

SHARD_SUFFIX = ".mshard"

# What the container stamps in the header so a reader knows how to parse a record's bytes.
_PAYLOAD_KIND = {
    FileFormat.SDF: PayloadKind.SDF,
    FileFormat.SMILES: PayloadKind.SMILES,
    FileFormat.PDBQT: PayloadKind.PDBQT,
}


def payload_kind_for(input_format: str) -> int:
    return _PAYLOAD_KIND.get(str(input_format or ""), PayloadKind.GENERIC_BYTES)


FORMAT_FOR_KIND = {kind: fmt for fmt, kind in _PAYLOAD_KIND.items()}


def mol_from_record(raw: str, input_format: str):
    """One record's raw text -> an RDKit molecule, or None. Shared with the shard pipeline,
    which is the other place that has to turn a stored record back into chemistry."""
    from rdkit import Chem

    if input_format == FileFormat.SDF:
        supplier = Chem.ForwardSDMolSupplier(io.BytesIO(str(raw).encode()), sanitize=True, removeHs=False)
        return next(iter(supplier), None)
    if input_format == FileFormat.SMILES:
        tokens = str(raw).strip().split(maxsplit=1)
        if not tokens:
            return None
        mol = Chem.MolFromSmiles(tokens[0])
        # The rest of the line is the record's name, and a shard has no row to keep it in:
        # identity has to travel inside the molecule or it is gone after the first transform.
        if mol is not None and len(tokens) > 1:
            mol.SetProp("_Name", tokens[1].strip())
        return mol
    return None


def smiles_record(raw: str, parse_config: dict[str, Any]) -> str:
    """One SMILES-table line -> `SMILES name`, which is what a shard record has to be.

    A shard header says SMILES and nothing else: the source's delimiter, column order and
    header do not travel with it. So the dialect is applied once, here, on the way in — and
    every reader downstream (`mol_from_record`, the preparation step, the cluster script) gets
    a line it can parse with no knowledge of where it came from.
    """
    from amdockvs.io.transformers.smiles import smiles_tokens

    tokens = smiles_tokens(raw, parse_config or {})
    if not tokens:
        return ""
    smiles_col = int((parse_config or {}).get("smiles_col") or 0)
    name_col = int((parse_config or {}).get("name_col") or 1)
    smiles = tokens[smiles_col] if 0 <= smiles_col < len(tokens) else tokens[0]
    name = tokens[name_col] if 0 <= name_col < len(tokens) else ""
    return f"{smiles} {name}".strip()


def filter_ligand_span(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One span of a source file -> the records that survive the prefilter.

    The worker half of the import: parsing (and therefore the filter) is where the CPU goes,
    so it runs in parallel; the surviving bytes travel back to the parent, which is the only
    process that writes shards.
    """
    batch = ImportBatchPayload.model_validate(payload)
    passes = htp_mol_filter(batch.prefilter, batch.molecule_kind)
    records: list[tuple[int, str]] = []
    for record in iter_record_span(
        file_path=batch.file_path,
        input_format=batch.input_format,
        offset=batch.span_offset,
        end=batch.span_end,
        first_index=batch.span_first_index,
    ):
        raw = str(record.get("raw") or "")
        if batch.input_format == FileFormat.SMILES:
            raw = smiles_record(raw, dict(batch.parse_config or {}))
        if passes is not None:
            mol = mol_from_record(raw, batch.input_format)
            if mol is None or not passes(cull_mol(mol, batch)):
                continue
        records.append((int(record["source_index"]), raw))
    return [
        {
            "source": str(batch.file_path),
            "input_format": batch.input_format,
            "records": records,
        }
    ]


class ShardQueueWriter:
    """The parent-side queue: survivors in, uniform shards out.

    Replaces the job's output sink (a shard is a file plus a row, and only this side of the
    process boundary can decide where a record lands). Runs on the executor's result thread,
    one shard at a time, which is what buys exact record counts and correlative ids.

    ponytail: not idempotent, by decision. A retried span is re-filtered and lands wherever
    the queue happens to be, so shard boundaries differ between runs and a re-import appends
    rather than upserting — a failed import is restarted from the beginning, not resumed.
    Resuming would need the queue to checkpoint (span -> shard) receipts.
    """

    def __init__(
        self,
        *,
        project_db: Any,
        shard_dir: str | Path,
        shard_size: int,
        generation_id: int | None = None,
    ) -> None:
        self._project_db = project_db
        self._shard_dir = Path(shard_dir).expanduser().resolve()
        self._shard_dir.mkdir(parents=True, exist_ok=True)
        # Opened by the caller (io/api.py), like the chemistry step's. An import *adds* to the
        # library, so it joins the active generation rather than forking one: only a rewrite
        # has a previous state worth being able to fall back to.
        self._generation_id = generation_id
        self._shard_size = max(1, int(shard_size))
        # One bucket per (source, format): mixing two files in one shard would leave the
        # `source` of its inventory row a lie.
        self._pending: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._next_id = 0
        self._shard_index = 0

    def handle(self, chunk_id: str, result: Any) -> None:
        for group in list(result or []):
            key = (str(group.get("source") or ""), str(group.get("input_format") or ""))
            bucket = self._pending.setdefault(key, [])
            bucket.extend((int(index), str(raw)) for index, raw in group.get("records") or [])
            while len(bucket) >= self._shard_size:
                self._write_shard(key, bucket[: self._shard_size])
                del bucket[: self._shard_size]

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed span contributes no records. Nothing to undo — see the class note."""

    def flush(self) -> None:
        """Called once the job is done: write what is left, one partial shard per source."""
        for key, bucket in list(self._pending.items()):
            if bucket:
                self._write_shard(key, bucket)
            self._pending.pop(key, None)

    def _write_shard(self, key: tuple[str, str], records: list[tuple[int, str]]) -> None:
        source, input_format = key
        shard_index = self._shard_index
        base_id = self._next_id
        self._shard_index += 1
        self._next_id += len(records)
        path = self._shard_dir / f"shard_{shard_index:05d}{SHARD_SUFFIX}"
        with ShardWriter(
            path,
            dataset_id=None,
            shard_id=shard_index,
            base_id=base_id,
            kind=payload_kind_for(input_format),
            serializer=Serializer.UTF8,
            slot_count=len(records),
            metadata={
                "source": source,
                "input_format": input_format,
                # Dense ids, so this is the only place the source ordinal survives.
                "source_indices": [index for index, _raw in records],
            },
        ) as writer:
            for offset, (_index, raw) in enumerate(records):
                writer.add(base_id + offset, raw.encode("utf-8"))
        self._insert_row(
            source=source,
            shard_index=shard_index,
            path=path,
            input_format=input_format,
            n_records=len(records),
        )

    def _insert_row(
        self, *, source: str, shard_index: int, path: Path, input_format: str, n_records: int
    ) -> None:
        from amdockvs.models import ScreeningShard

        now = datetime.now()
        with self._project_db.get_session() as session:
            session.add(
                ScreeningShard(
                    generation_id=self._generation_id,
                    source=source,
                    shard_index=shard_index,
                    path=str(path),
                    input_format=input_format,
                    n_records=n_records,
                    state=ShardState.PENDING,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()


def shard_queue_writer(
    *, project_db: Any, shard_dir: str | Path, shard_size: int, generation_id: int | None = None
) -> ShardQueueWriter:
    """Factory for `@job(result_handler_factory=...)`; the runtime passes the kwargs."""
    return ShardQueueWriter(
        project_db=project_db,
        shard_dir=shard_dir,
        shard_size=shard_size,
        generation_id=generation_id,
    )


class ShardGenerationWriter:
    """The result sink of any step that rewrites the library: new shards, new generation.

    Replaces an upsert on `(source, shard_index)`. That upsert repointed the inventory one
    chunk at a time, so a job that died at shard 600 of 1000 left a library that was 60% the
    new step and 40% the old one, with no way to tell which was which.

    Here the rows land in a generation nothing reads yet, and `flush()` — which MolSuite
    calls once, when the job actually completes — is the only thing that makes them the
    library. A failed job leaves an inert generation behind, not a corrupted one.
    """

    def __init__(self, *, project_db: Any, generation_id: int) -> None:
        self._project_db = project_db
        self._generation_id = int(generation_id)

    def handle(self, chunk_id: str, result: Any) -> None:
        from amdockvs.models import ScreeningShard

        rows = [dict(row) for row in (result or [])]
        if not rows:
            return
        now = datetime.now()
        with self._project_db.get_session() as session:
            for row in rows:
                session.add(
                    ScreeningShard(
                        generation_id=self._generation_id,
                        source=str(row.get("source") or ""),
                        shard_index=int(row.get("shard_index") or 0),
                        path=str(row.get("path") or ""),
                        input_format=str(row.get("input_format") or ""),
                        n_records=int(row.get("n_records") or 0),
                        state=str(row.get("state") or ShardState.READY),
                        error=str(row.get("error") or ""),
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()

    def on_error(self, chunk_id: str, error: str) -> None:
        """A failed shard contributes no row, so it is simply absent from the generation.

        ponytail: the generation is still activated with a hole in it. Whether a partial
        rewrite should be rejected outright is a policy question — today a failed shard is
        reported per chunk and the user re-runs the step, which refills it.
        """

    def flush(self) -> None:
        """The switch. One UPDATE, and the grandparent is forgotten.

        Refuses an empty generation. A step where every molecule failed reports a clean chunk
        per shard — `n_records=0` is a valid shard — so without this check the job completes
        and the switch silently replaces the library with nothing. That is the one outcome a
        rewrite must never be allowed to reach.
        """
        from amdockvs.molecules.storage import ShardStore, activate_generation, shard_scope_spec

        spec = shard_scope_spec(state=None)
        store = ShardStore(self._project_db)
        written = store.record_count(replace(spec, filters={**spec.filters, "generation_id": self._generation_id}))
        if written <= 0:
            raise RuntimeError(
                f"Shard generation {self._generation_id} ended with 0 molecules: refusing to "
                "replace the library with an empty one. Every molecule failed the step; the "
                "previous generation is still the library."
            )
        activate_generation(self._project_db, self._generation_id)


def shard_generation_writer(*, project_db: Any, generation_id: int) -> ShardGenerationWriter:
    """Factory for `@job(result_handler_factory=...)`; the runtime passes the kwargs."""
    return ShardGenerationWriter(project_db=project_db, generation_id=generation_id)


__all__ = [
    "FORMAT_FOR_KIND",
    "SHARD_SUFFIX",
    "ShardGenerationWriter",
    "ShardQueueWriter",
    "shard_generation_writer",
    "filter_ligand_span",
    "mol_from_record",
    "payload_kind_for",
    "shard_queue_writer",
    "smiles_record",
]
