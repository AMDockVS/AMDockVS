"""Materializing an SDF/MOL batch."""
from __future__ import annotations

import io
from typing import Any, Callable, Iterable

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.formats import mol_from_mol2_block
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.core.vocab import FileFormat, MoleculeType
from amdockvs.io.import_stats import FILTERED_PREFILTER, IMPORTED, UNREADABLE, bump
from amdockvs.io.rows import active_small_molecule_criteria, cull_mol, htp_mol_filter
from amdockvs.core.payloads import series_for, structure_payload
from amdockvs.core.paths import molecule_storage_key
from amdockvs.io.transformers.rows import (
    _apply_sdf_activity,
    _build_row,
    _finalize_ligand_row_from_mol,
    share_current,
    _progress_update,
    _source_properties_from_mapping,
    _split_fragment_rows,
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
        if htp_passes is not None and not htp_passes(cull_mol(mol, batch)):
            bump(tally, FILTERED_PREFILTER)
            _progress_update(progress_cb, index, total_entries)
            continue
        row = _build_row(
            source_file=batch.file_path,
            source_index=source_index,
            name=name,
            n_atoms=mol.GetNumAtoms(),
            import_batch_id=batch.import_batch_id,
            stored=structure_payload(mol_block, "sdf", series_for(batch.primary_role or batch.kind)),
            current=None,
            metadata=molecule_state_metadata(mol),
            molecule_kind=batch.molecule_kind,
            primary_role=batch.primary_role,
            primary_context=batch.primary_context,
        )
        row["source_properties"] = _source_properties_from_mapping(entry.get("source_properties"))
        _apply_sdf_activity(row, getattr(batch, "prefilter", None))
        if str(batch.primary_role or batch.kind).strip().lower() == "ligand" and str(batch.molecule_kind or "").strip().lower() == MoleculeType.SMALL_MOLECULE:
            row, reason = _finalize_ligand_row_from_mol(
                row=row,
                mol=mol,
                prefilter=batch.prefilter,
                criteria=criteria,
                molecule_kind=batch.molecule_kind,
            )
            if reason is not None:
                bump(tally, reason)
                _progress_update(progress_cb, index, total_entries)
                continue
        else:
            share_current(row, structure_payload(mol_block, "sdf", series_for(batch.primary_role or batch.kind)))
        bump(tally, IMPORTED)
        yield row
        yield from _split_fragment_rows(
            batch=batch, source_index=source_index, name=name, mol=mol,
            parent_key=molecule_storage_key(batch.primary_role or batch.kind, batch.file_path, source_index),
            criteria=criteria,
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
