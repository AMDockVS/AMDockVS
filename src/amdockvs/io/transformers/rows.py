"""Shared row building for every import format.

One imported molecule becomes one row here — path, format, descriptors, metadata — no
matter whether it came from an SDF, a SMILES line or a PDB. The format modules
(`sdf`, `smiles`, `structures`) call in; nothing here knows which one is running.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.payloads import ImportBatchPayload, ImportPrefilterPolicy
from amdockvs.models import MoleculeRecord
from amdockvs.models.molecules import MoleculeUsageClass
from amdockvs.io.import_stats import IMPORTED, bump
from amdockvs.io.rows import evaluate_ligand_mol, ligand_row_fields
from amdockvs.molecules.fragments import extra_fragment_molecules, write_fragment_files
from amdockvs.core.paths import managed_paths_for_source


def _build_row(
    *,
    project_root: Path | None = None,
    source_file: Path,
    source_index: int,
    name: str,
    n_atoms: int,
    input_format: str,
    stored_path: Path,
    current_path: Path | None,
    metadata: dict[str, Any],
    molecule_kind: str,
    primary_role: str,
    primary_context: str,
    usage_class: str = "",
) -> dict[str, Any]:
    now = datetime.now()
    # ponytail: usage_class is derived from the context unless the caller states one. Only the
    # `general` context is the screening library; anything imported into a named context
    # (reference, cocrystal, activity) is curated, and `general_ligand_count` must not see it.
    resolved_usage = str(usage_class or "") or (
        MoleculeUsageClass.GENERAL
        if str(primary_context or "general").strip().lower() in ("", "general")
        else MoleculeUsageClass.REFERENCE
    )
    metadata_map = dict(metadata or {})
    effective_project_root = project_root or _infer_project_root(stored_path)
    state = metadata_map.get("state") if isinstance(metadata_map.get("state"), dict) else {}
    molecule_row = MoleculeRecord.build_row(
        project_root=effective_project_root,
        source_file=source_file,
        source_index=int(source_index),
        name=str(name or f"{source_file.stem}_{int(source_index)}"),
        molecule_type=str(molecule_kind or "unknown"),
        n_atoms=max(0, int(n_atoms)),
        input_format=str(input_format or ""),
        stored_path=stored_path,
        current_path=current_path,
        current_model_index=None,
        extra_data=metadata_map,
        created_at=now,
        primary_context=str(primary_context or ""),
        usage_class=resolved_usage,
    )
    return {
        **molecule_row,
        "molecule_kind": str(molecule_row.get("molecule_type") or molecule_kind or "unknown"),
        "primary_role": str(primary_role or ""),
        "smiles": str(metadata_map.get("smiles") or ""),
        "sequence_1d": str(metadata_map.get("sequence_1d") or ""),
        "status_flags": 0,
        "metadata_json": json.dumps(metadata_map, ensure_ascii=True),
        "source_properties": [],
        "complex_spec": None,
        "activity_spec": None,
        "binding_site_specs": [],
        "has_3d": bool(state.get("has_3d", bool(molecule_row.get("has_3d")))),
        "has_hs": bool(state.get("has_hs", bool(molecule_row.get("has_hs")))),
        "is_minimized": bool(state.get("is_minimized", bool(molecule_row.get("is_minimized")))),
        "conformer_count": max(0, int(state.get("conformer_count", int(molecule_row.get("conformer_count") or 0)) or 0)),
    }

def _finalize_ligand_row_from_mol(
    *,
    row: dict[str, Any],
    mol,
    storage_root: Path,
    role: str,
    storage_key: str,
    project_root: Path,
    current_path: Path,
    prefilter: ImportPrefilterPolicy | None = None,
    stored_path: Path | None = None,
    mol_block: str = "",
    criteria=None,
    molecule_kind: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Writes the files of a ligand that already survived every filter.

    The decision is pure and lives in `io/rows.py`; only the I/O is left here, and it runs
    afterwards, so a late rejection still costs zero files.
    """
    from rdkit import Chem

    decision, reason = evaluate_ligand_mol(
        mol=mol,
        metadata=row.get("extra_data"),
        storage_root=storage_root,
        role=role,
        storage_key=storage_key,
        project_root=project_root,
        prefilter=prefilter,
        criteria=criteria,
        molecule_kind=molecule_kind,
    )
    if decision is None:
        return None, reason

    if stored_path is not None:
        stored_path.write_text(mol_block, encoding="utf-8")
    write_fragment_files(decision.fragment_info, decision.fragment_mols, project_root=project_root)
    current_path.write_text(Chem.MolToMolBlock(decision.kept_mol), encoding="utf-8")
    return ligand_row_fields(row, decision, current_path_rel=str(current_path.relative_to(project_root))), None

def _progress_update(progress_cb: Callable[[float], None] | None, completed: int, total: int) -> None:
    if progress_cb is None or total <= 0:
        return
    progress_cb((max(0, completed) / max(1, total)) * 100.0)

def _infer_project_root(stored_path: Path) -> Path:
    resolved = stored_path.expanduser().resolve()
    parts = resolved.parts
    if "data" in parts:
        data_index = parts.index("data")
        if data_index > 0:
            return Path(*parts[:data_index])
    return resolved.parent

def _project_root_from_storage_dir(storage_dir: Path) -> Path:
    resolved = storage_dir.expanduser().resolve()
    parts = resolved.parts
    if "data" in parts:
        data_index = parts.index("data")
        if data_index > 0:
            return Path(*parts[:data_index])
    return resolved.parent

def _split_fragment_rows(
    *,
    batch: ImportBatchPayload,
    project_root: Path,
    source_index: int,
    name: str,
    mol,
    parent_key: str,
    input_format: str,
    criteria,
    source_properties: Any,
    tally: dict[str, int],
) -> Iterable[dict[str, Any]]:
    """One row per *discarded* organic fragment, when `prefilter.split_fragments` is on.

    The record's own row already carries the kept fragment; these are the co-components that
    would otherwise be lost. They share the source record but need their own storage key, hence
    `variant`. No activity is copied onto them: the assay measured the parent record, not a salt.
    """
    from rdkit import Chem

    if not bool(getattr(batch.prefilter, "split_fragments", False)):
        return
    for fragment_index, fragment in extra_fragment_molecules(mol):
        variant = f"frag{fragment_index:02d}"
        paths = managed_paths_for_source(
            storage_root=batch.storage_dir,
            role=batch.primary_role or batch.kind,
            source_file=batch.file_path,
            source_index=source_index,
            original_suffix=".sdf",
            current_suffix=".sdf",
            variant=variant,
        )
        fragment.SetProp("_Name", f"{name} [{variant}]")
        mol_block = Chem.MolToMolBlock(fragment)
        row = _build_row(
            project_root=project_root,
            source_file=batch.file_path,
            source_index=source_index,
            name=f"{name} [{variant}]",
            n_atoms=fragment.GetNumAtoms(),
            input_format=input_format,
            stored_path=paths["original_path"],
            current_path=paths["current_path"],
            # Points back at the sibling that kept the record: same source index, and the
            # fragment_index the parent's own `fragmentation.components` lists it under.
            metadata={
                **molecule_state_metadata(fragment),
                "split_from": {
                    "source_index": int(source_index),
                    "parent_key": str(parent_key),
                    "parent_name": str(name),
                    "fragment_index": int(fragment_index),
                },
            },
            molecule_kind=batch.molecule_kind,
            primary_role=batch.primary_role,
            primary_context=batch.primary_context,
        )
        row["source_properties"] = _source_properties_from_mapping(source_properties)
        row, reason = _finalize_ligand_row_from_mol(
            row=row,
            mol=fragment,
            storage_root=batch.storage_dir,
            role=batch.primary_role or batch.kind,
            storage_key=str(paths["key"]),
            project_root=project_root,
            current_path=paths["current_path"],
            prefilter=batch.prefilter,
            stored_path=paths["original_path"],
            mol_block=mol_block,
            criteria=criteria,
            molecule_kind=batch.molecule_kind,
        )
        if reason is not None:
            bump(tally, reason)
            continue
        bump(tally, IMPORTED)
        yield row

def _source_properties_from_mapping(raw_mapping: Any) -> list[dict[str, str]]:
    mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
    rows: list[dict[str, str]] = []
    for key, value in mapping.items():
        normalized_key = str(key or "").strip()
        normalized_value = str(value or "").strip()
        if not normalized_key or not normalized_value:
            continue
        rows.append({"key": normalized_key, "value_text": normalized_value})
    return rows

def _apply_sdf_activity(row: dict[str, Any], prefilter) -> None:
    """Set the row's activity spec(s) from the import prefilter's tag/column mapping. The single
    'Activity from tag' maps to one endpoint (row['activity_spec']); the multi-column mapping (many
    CSV columns → many endpoints, e.g. Tox21) maps to row['activity_specs'] (a list). The
    materializer persists each as an ActivityRecord."""
    if prefilter is None:
        return
    source_properties = row.get("source_properties") or []
    multi = getattr(prefilter, "activity_specs_from_properties", None)
    if multi is not None:
        specs = multi(source_properties)
        if specs:
            row["activity_specs"] = specs
            row["has_activity"] = True
        return
    # older prefilter without the multi-column builder
    if row.get("activity_spec"):
        return
    builder = getattr(prefilter, "activity_spec_from_properties", None)
    if builder is None:
        return
    spec = builder(source_properties)
    if spec is not None:
        row["activity_spec"] = spec
        row["has_activity"] = True

def _maybe_parse_activity_spec(activity_text: str, *, source_file: Path, import_mode: str) -> dict[str, Any] | None:
    text = str(activity_text or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return {
        "value": value,
        "unit": "",
        "activity_type": "activity",
        "description": f"{import_mode} cocrystal activity",
        "source": source_file.name,
    }
