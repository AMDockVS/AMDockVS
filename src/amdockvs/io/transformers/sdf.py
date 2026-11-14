"""Materializing an SDF/MOL batch."""
from __future__ import annotations

import io
from typing import Any, Callable, Iterable

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.formats import mol_from_mol2_block
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.core.vocab import FileFormat
from amdockvs.io.import_stats import UNREADABLE, bump
from amdockvs.io.rows import active_small_molecule_criteria, htp_mol_filter
from amdockvs.core.payloads import series_for, structure_payload
from amdockvs.core.paths import molecule_storage_key
from amdockvs.io.transformers.rows import (
    _apply_sdf_activity,
    _build_row,
    _progress_update,
    _source_properties_from_mapping,
    record_rows,
)


def _materialize_sdf_rows(
    *,
    batch: ImportBatchPayload,
    entries: list[dict[str, Any]],
    progress_cb: Callable[[float], None] | None = None,
    tally: dict[str, int] | None = None,
) -> Iterable[dict[str, Any]]:
    from rdkit import Chem

    tally = tally if tally is not None else {}
    criteria = active_small_molecule_criteria(batch.prefilter, batch.molecule_kind)
    htp_passes = htp_mol_filter(batch.prefilter, batch.molecule_kind)
    total_entries = len(entries)
    for index, entry in enumerate(entries, start=1):
        source_index = int(entry.get("source_index") or 0)
        raw = entry.get("raw")
        if raw is not None:
            # Worker-side parse of the raw SDF/mol2 record (the feed only sliced text).
            if batch.input_format == FileFormat.MOL2:
                mol = mol_from_mol2_block(str(raw))
            else:
                mol = next(iter(Chem.ForwardSDMolSupplier(io.BytesIO(str(raw).encode()), sanitize=True, removeHs=False)), None)
            if mol is None:
                bump(tally, UNREADABLE)
                _progress_update(progress_cb, index, total_entries)
                continue
            mol_block = Chem.MolToMolBlock(mol)
            name = mol.GetProp("_Name") if mol.HasProp("_Name") else f"{batch.file_path.stem}_{source_index}"
            entry = {**entry, "source_properties": _mol_source_properties(mol)}
        else:
            mol_block = str(entry.get("mol_block") or "")
            name = str(entry.get("name") or f"{batch.file_path.stem}_{source_index}")
            mol = Chem.MolFromMolBlock(mol_block, sanitize=True, removeHs=False)
            if mol is None:
                bump(tally, UNREADABLE)
                _progress_update(progress_cb, index, total_entries)
                continue
        stored = structure_payload(mol_block, "sdf", series_for(batch.primary_role or batch.kind))

        def make_row(mol=mol, name=name, source_index=source_index, entry=entry, stored=stored):
            row = _build_row(
                source_file=batch.file_path,
                source_index=source_index,
                name=name,
                n_atoms=mol.GetNumAtoms(),
                import_batch_id=batch.import_batch_id,
                stored=stored,
                current=None,
                metadata=molecule_state_metadata(mol),
                molecule_kind=batch.molecule_kind,
                primary_role=batch.primary_role,
                primary_context=batch.primary_context,
            )
            row["source_properties"] = _source_properties_from_mapping(entry.get("source_properties"))
            _apply_sdf_activity(row, getattr(batch, "prefilter", None))
            return row

        yield from record_rows(
            batch=batch, mol=mol, name=name, source_index=source_index, make_row=make_row,
            current=stored, criteria=criteria, htp_passes=htp_passes,
            parent_key=molecule_storage_key(batch.primary_role or batch.kind, batch.file_path, source_index),
            source_properties=entry.get("source_properties"), tally=tally,
        )
        _progress_update(progress_cb, index, total_entries)

def _mol_source_properties(mol) -> dict[str, str]:
    properties: dict[str, str] = {}
    for key in mol.GetPropNames(includePrivate=False, includeComputed=False):
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key == "_Name":
            continue
        value_text = str(mol.GetProp(key) or "").strip()
        if value_text:
            properties[normalized_key] = value_text
    return properties
