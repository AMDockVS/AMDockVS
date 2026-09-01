from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterator, Mapping

from amdockvs.io._common import normalize_context, normalize_kind, normalize_molecule_kind, normalize_role
from amdockvs.io.parsers import iter_record_spans
from amdockvs.io.payloads import ImportBatchPayload, ImportPrefilterPolicy

# Byte-based chunking: each chunk carries ~this many bytes of RAW input. Because
# chunks are sized by bytes (not molecule count), total_chunks ≈ file_size /
# IMPORT_CHUNK_BYTES is knowable O(1) up front, and progress tracks bytes
# consumed regardless of how many molecules survive filtering.
#
# 4MB (not 1MB): the single-threaded executor loop has a fixed per-chunk cost
# (payload encode + dispatch + result handling), so fewer/larger chunks raise
# molecule throughput and keep the CPU pool fed. Measured ~2.4x on a multi-file
# HDD import going 1MB→8MB; 4MB is the balance vs per-chunk memory/retry size.
IMPORT_CHUNK_BYTES = 4 * 1024 * 1024

# Ramp-up: the first chunks are deliberately tiny so the first molecules reach the table in
# well under a second. A full 1000-record chunk takes ~5-7s to parse+store, which is why an
# import used to look frozen at the start. Throughput is unaffected — only these first
# ~300 records are chunked small, the rest use the caller's batch_size.
IMPORT_RAMP_UP_SIZES = (32, 256)


def estimate_total_chunks(file_size_bytes: int) -> int:
    return max(1, math.ceil(max(0, int(file_size_bytes)) / IMPORT_CHUNK_BYTES))


def estimate_record_chunks(records: int, batch_size: int) -> int:
    """Chunks the record cap alone yields for ``records``, ramp-up included. This must not
    exceed what the feed really emits: a job whose declared total is unreachable never
    completes."""
    normalized_batch_size = max(1, int(batch_size))
    remaining = max(0, int(records))
    chunks = 0
    for ramp in IMPORT_RAMP_UP_SIZES:
        if remaining <= 0:
            break
        remaining -= min(remaining, normalized_batch_size, ramp)
        chunks += 1
    return max(1, chunks + math.ceil(remaining / normalized_batch_size))


def stream_import_payload_batches(
    *,
    kind: str,
    file_path: str | Path,
    storage_dir: str | Path,
    batch_size: int,
    primary_role: str = "",
    primary_context: str = "general",
    molecule_kind: str = "unknown",
    prefilter: ImportPrefilterPolicy | Mapping[str, Any] | None = None,
    extra_data_patch: Mapping[str, Any] | None = None,
    binding_site_specs: list[Mapping[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    normalized_kind = normalize_kind(kind)
    source_path = Path(file_path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {source_path}")

    normalized_batch_size = max(1, batch_size)
    normalized_storage_dir = Path(storage_dir).expanduser().resolve()
    normalized_prefilter = None if prefilter is None else ImportPrefilterPolicy.model_validate(prefilter)
    normalized_extra_data_patch = dict(extra_data_patch or {})
    normalized_binding_site_specs = [dict(item) for item in list(binding_site_specs or [])]
    # Only a cheap byte-level split here — RDKit parsing happens in the worker. And only the
    # offsets survive the split: the text is re-read from the file in the worker.
    input_format, parse_config, record_iter = iter_record_spans(kind=normalized_kind, file_path=source_path)

    def _payload(records: list[dict[str, Any]]) -> dict[str, Any]:
        spanned = [record for record in records if "offset" in record]
        return ImportBatchPayload(
            kind=normalized_kind,
            file_path=source_path,
            storage_dir=normalized_storage_dir,
            input_format=input_format,
            primary_role=normalize_role(primary_role),
            primary_context=normalize_context(primary_context),
            molecule_kind=normalize_molecule_kind(molecule_kind, kind=normalized_kind),
            prefilter=normalized_prefilter,
            extra_data_patch=normalized_extra_data_patch,
            binding_site_specs=normalized_binding_site_specs,
            span_offset=int(spanned[0]["offset"]) if spanned else -1,
            span_end=int(spanned[-1]["end"]) if spanned else -1,
            span_first_index=int(spanned[0]["source_index"]) if spanned else 0,
            span_count=len(spanned),
            records=[] if spanned else list(records),
            parse_config=dict(parse_config),
        ).model_dump(mode="json")

    batch: list[dict[str, Any]] = []
    batch_bytes = 0
    emitted = 0
    for record in record_iter:
        record_bytes = (
            int(record["end"]) - int(record["offset"])
            if "offset" in record
            else len(str(record.get("raw") or ""))
        )
        ramp = IMPORT_RAMP_UP_SIZES[emitted] if emitted < len(IMPORT_RAMP_UP_SIZES) else normalized_batch_size
        record_cap = min(normalized_batch_size, ramp)
        # Close the current chunk on a byte budget (primary) or a record-count
        # safety cap (so tiny SMILES lines can't build a giant task).
        if batch and (batch_bytes + record_bytes > IMPORT_CHUNK_BYTES or len(batch) >= record_cap):
            yield _payload(batch)
            emitted += 1
            batch = []
            batch_bytes = 0
        batch.append(dict(record))
        batch_bytes += record_bytes
    if batch:
        yield _payload(batch)


__all__ = [
    "stream_import_payload_batches",
    "estimate_total_chunks",
    "estimate_record_chunks",
    "IMPORT_CHUNK_BYTES",
    "IMPORT_RAMP_UP_SIZES",
]
