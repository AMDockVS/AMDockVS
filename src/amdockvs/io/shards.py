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
        tokens = str(raw).strip().split()
        return Chem.MolFromSmiles(tokens[0]) if tokens else None
    return None


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

    def __init__(self, *, project_db: Any, shard_dir: str | Path, shard_size: int) -> None:
        self._project_db = project_db
        self._shard_dir = Path(shard_dir).expanduser().resolve()
        self._shard_dir.mkdir(parents=True, exist_ok=True)
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


def shard_queue_writer(*, project_db: Any, shard_dir: str | Path, shard_size: int) -> ShardQueueWriter:
    """Factory for `@job(result_handler_factory=...)`; the runtime passes the kwargs."""
    return ShardQueueWriter(project_db=project_db, shard_dir=shard_dir, shard_size=shard_size)


__all__ = [
    "FORMAT_FOR_KIND",
    "SHARD_SUFFIX",
    "ShardQueueWriter",
    "filter_ligand_span",
    "mol_from_record",
    "payload_kind_for",
    "shard_queue_writer",
]
