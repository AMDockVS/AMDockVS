"""Materializing a SMILES file or a CSV/SMI column."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.models.molecules import MoleculeType
from amdockvs.io.import_stats import FILTERED_PREFILTER, IMPORTED, UNREADABLE, bump
from amdockvs.io.rows import active_small_molecule_criteria, cull_mol, htp_mol_filter
from amdockvs.core.paths import managed_paths_for_source
from amdockvs.io.transformers.rows import (
    _source_properties_from_mapping,
    _apply_sdf_activity,
    _build_row,
    _finalize_ligand_row_from_mol,
    _progress_update,
    _project_root_from_storage_dir,
    _split_fragment_rows,
)


def _materialize_smiles_rows(
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
    project_root = _project_root_from_storage_dir(batch.storage_dir)
    config = dict(batch.parse_config or {})
    for index, entry in enumerate(entries, start=1):
        source_index = int(entry.get("source_index") or 0)
        raw = entry.get("raw")
        if raw is not None:
            # Worker-side parse of the raw SMILES line (feed only split lines).
            parsed = _parse_smiles_line(str(raw), config, source_index=source_index, stem=batch.file_path.stem)
            smiles, name, mol, mol_block = parsed["smiles"], parsed["name"], parsed["mol"], ""
            entry = {**entry, "source_properties": parsed["source_properties"]}
            if mol is None:
                bump(tally, UNREADABLE)
                _progress_update(progress_cb, index, total_entries)
                continue
            smiles = Chem.MolToSmiles(mol, canonical=False)
        else:
            smiles = str(entry.get("smiles") or "")
            name = str(entry.get("name") or f"{batch.file_path.stem}_{source_index}")
            mol_block = str(entry.get("mol_block") or "")
            mol = Chem.MolFromMolBlock(mol_block, sanitize=True, removeHs=False) if mol_block else None
            if mol is None:
                mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                bump(tally, UNREADABLE)
                _progress_update(progress_cb, index, total_entries)
                continue
        if htp_passes is not None and not htp_passes(cull_mol(mol, batch)):
            bump(tally, FILTERED_PREFILTER)
            _progress_update(progress_cb, index, total_entries)
            continue
        paths = managed_paths_for_source(
            storage_root=batch.storage_dir,
            role=batch.primary_role or batch.kind,
            source_file=batch.file_path,
            source_index=source_index,
            original_suffix=".sdf",
            current_suffix=".sdf",
        )
        stored_path = paths["original_path"]
        current_path = paths["current_path"]
        mol.SetProp("_Name", name)
        resolved_mol_block = mol_block or Chem.MolToMolBlock(mol)
        row = _build_row(
            project_root=project_root,
            source_file=batch.file_path,
            source_index=source_index,
            name=name,
            n_atoms=mol.GetNumAtoms(),
            input_format="smiles",
            stored_path=stored_path,
            current_path=current_path,
            metadata={**molecule_state_metadata(mol), "smiles": smiles},
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
                storage_root=batch.storage_dir,
                role=batch.primary_role or batch.kind,
                storage_key=str(paths["key"]),
                project_root=project_root,
                current_path=current_path,
                prefilter=batch.prefilter,
                stored_path=stored_path,
                mol_block=resolved_mol_block,
                criteria=criteria,
                molecule_kind=batch.molecule_kind,
            )
            if reason is not None:
                bump(tally, reason)
                _progress_update(progress_cb, index, total_entries)
                continue
        else:
            stored_path.write_text(resolved_mol_block, encoding="utf-8")
            current_path.write_text(resolved_mol_block, encoding="utf-8")
        bump(tally, IMPORTED)
        yield row
        yield from _split_fragment_rows(
            batch=batch, project_root=project_root, source_index=source_index, name=name, mol=mol,
            parent_key=str(paths["key"]), input_format="smiles", criteria=criteria,
            source_properties=entry.get("source_properties"), tally=tally,
        )
        _progress_update(progress_cb, index, total_entries)

def _parse_smiles_line(raw: str, config: dict[str, Any], *, source_index: int, stem: str) -> dict[str, Any]:
    """Parse one raw SMILES-table line into {smiles, name, mol, source_properties}.

    Mirrors the columns the feed sniffed (delimiter/smiles_col/name_col/header),
    surfacing extra header columns as source properties like SDF tags do."""
    from rdkit import Chem

    delimiter = str(config.get("delimiter") or " ")
    smiles_col = int(config.get("smiles_col") or 0)
    name_col = int(config.get("name_col") or 1)
    header_names = list(config.get("header_names") or [])
    tokens = [t.strip() for t in (raw.split(",") if delimiter == "," else raw.split())]
    smiles = tokens[smiles_col] if 0 <= smiles_col < len(tokens) else ""
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    name = tokens[name_col] if (0 <= name_col < len(tokens) and tokens[name_col]) else f"{stem}_{source_index}"
    source_properties: dict[str, str] = {}
    for col_index, column_name in enumerate(header_names):
        if col_index in (smiles_col, name_col):
            continue
        if col_index < len(tokens) and tokens[col_index] and str(column_name or "").strip():
            source_properties[str(column_name).strip()] = tokens[col_index]
    return {"smiles": smiles, "name": name, "mol": mol, "source_properties": source_properties}
