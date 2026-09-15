"""Materializing a FASTA file: one sequence-only protein per record, no structure yet."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from amdockvs.core.vocab import FileFormat
from amdockvs.io.import_stats import IMPORTED, UNREADABLE, bump
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.transformers.rows import _build_row, _progress_update, _project_root_from_storage_dir
from amdockvs.models.molecules import MoleculeType

_STATE = {"has_3d": False, "has_hs": False, "is_minimized": False, "conformer_count": 0}


def _materialize_fasta_rows(
    *,
    batch: ImportBatchPayload,
    entries: list[dict[str, Any]],
    progress_cb: Callable[[float], None] | None = None,
    tally: dict[str, int] | None = None,
) -> Iterable[dict[str, Any]]:
    tally = tally if tally is not None else {}
    project_root = _project_root_from_storage_dir(batch.storage_dir)
    total_entries = len(entries)
    for index, entry in enumerate(entries, start=1):
        _progress_update(progress_cb, index, total_entries)
        sequence = str(entry.get("raw") or "").strip()
        if not sequence:
            bump(tally, UNREADABLE)
            continue
        source_index = int(entry.get("source_index") or 0)
        row = _build_row(
            project_root=project_root,
            source_file=batch.file_path,
            source_index=source_index,
            name=str(entry.get("name") or f"{batch.file_path.stem}_{source_index}"),
            n_atoms=0,
            input_format=FileFormat.FASTA,
            stored_path=None,
            current_path=None,
            metadata={"sequence_1d": sequence, "state": dict(_STATE)},
            molecule_kind=batch.molecule_kind or MoleculeType.PROTEIN,
            primary_role=batch.primary_role or batch.kind,
            primary_context=batch.primary_context,
        )
        row["sequence_aa"] = sequence
        bump(tally, IMPORTED)
        yield row
