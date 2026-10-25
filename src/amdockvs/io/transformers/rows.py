"""Shared row building for every import format.

One imported molecule becomes one row here — structure payloads, format, descriptors, metadata —
no matter whether it came from an SDF, a SMILES line or a PDB. The format modules
(`sdf`, `smiles`, `structures`) call in; nothing here knows which one is running.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from ms_flow.payloads import Payload

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.payloads import ImportBatchPayload, ImportPrefilterPolicy
from amdockvs.core.payloads import structure_payload
from amdockvs.core.rows import molecule_row
from amdockvs.core.vocab import MoleculeUsageClass
from amdockvs.io.import_stats import IMPORTED, bump
from amdockvs.io.rows import evaluate_ligand_mol, ligand_row_fields
from amdockvs.molecules.fragments import extra_fragment_molecules


def _build_row(
    *,
    source_file: Path,
    source_index: int,
    name: str,
    n_atoms: int,
    import_batch_id: int | None,
    stored: Payload | None,
    current: Payload | None,
    metadata: dict[str, Any],
    molecule_kind: str,
    primary_role: str,
    primary_context: str,
    usage_class: str = "",
) -> dict[str, Any]:
    # ponytail: usage_class is derived from the context unless the caller states one. Only the
    # `general` context is the screening library; anything imported into a named context
    # (reference, cocrystal, activity) is curated, and `general_ligand_count` must not see it.
    resolved_usage = str(usage_class or "") or (
        MoleculeUsageClass.GENERAL
        if str(primary_context or "general").strip().lower() in ("", "general")
        else MoleculeUsageClass.REFERENCE
    )
    metadata_map = dict(metadata or {})
    state = metadata_map.get("state") if isinstance(metadata_map.get("state"), dict) else {}
    built = molecule_row(
        source_file=source_file,
        source_index=int(source_index),
        name=str(name or f"{source_file.stem}_{int(source_index)}"),
        molecule_type=str(molecule_kind or "unknown"),
        n_atoms=max(0, int(n_atoms)),
        import_batch_id=import_batch_id,
        stored_payload_id=stored,
        current_payload_id=current,
        extra_data=metadata_map,
        usage_class=resolved_usage,
    )
    return {
        **built,
        "source_file": str(source_file.resolve()),  # graph refs only; the batch says the source
        "molecule_kind": str(built.get("molecule_type") or molecule_kind or "unknown"),
        "primary_role": str(primary_role or ""),
        "status_flags": 0,
        "metadata_json": json.dumps(metadata_map, ensure_ascii=True),
        "source_properties": [],
        "complex_spec": None,
        "activity_spec": None,
        "binding_site_specs": [],
        "has_3d": bool(state.get("has_3d", bool(built.get("has_3d")))),
        "has_hs": bool(state.get("has_hs", bool(built.get("has_hs")))),
        "is_minimized": bool(state.get("is_minimized", bool(built.get("is_minimized")))),
        "conformer_count": max(0, int(state.get("conformer_count", int(built.get("conformer_count") or 0)) or 0)),
    }

def share_current(row: dict[str, Any], current: Payload) -> None:
    """Set the row's current form; NULL when it is the stored bytes (`molecule_payload_id`)."""
    stored = row.get("stored_payload_id")
    same = stored is not None and stored.kind == current.kind and stored.data == current.data
    row["current_payload_id"] = None if same else current


def _finalize_ligand_row_from_mol(
    *,
    row: dict[str, Any],
    mol,
    prefilter: ImportPrefilterPolicy | None = None,
    criteria=None,
    molecule_kind: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Builds the current-structure payload of a ligand that already survived every filter.

    The decision is pure and lives in `io/rows.py`; only the payload is left here, and it is
    built afterwards, so a late rejection still costs nothing. The input is whatever the row
    already holds in `stored_payload_id`.
    """
    from rdkit import Chem

    decision, reason = evaluate_ligand_mol(
        mol=mol,
        metadata=row.get("extra_data"),
        prefilter=prefilter,
        criteria=criteria,
        molecule_kind=molecule_kind,
    )
    if decision is None:
        return None, reason

    stored, kept = row.get("stored_payload_id"), decision.kept_mol
    if stored is not None and stored.kind == "smi" and not kept.GetNumConformers():
        # No coordinates to keep: a SMILES says all a block would (D6); the stored one when unchanged.
        row["current_payload_id"] = None if kept is mol else structure_payload(
            Chem.MolToSmiles(kept), "smi", stored.series)
    else:
        share_current(row, structure_payload(Chem.MolToMolBlock(kept), "sdf"))
    return ligand_row_fields(row, decision), None

def _progress_update(progress_cb: Callable[[float], None] | None, completed: int, total: int) -> None:
    if progress_cb is None or total <= 0:
        return
    progress_cb((max(0, completed) / max(1, total)) * 100.0)

def _split_fragment_rows(
    *,
    batch: ImportBatchPayload,
    source_index: int,
    name: str,
    mol,
    parent_key: str,
    criteria,
    source_properties: Any,
    tally: dict[str, int],
) -> Iterable[dict[str, Any]]:
    """One row per *discarded* organic fragment, when `prefilter.split_fragments` is on.

    The record's own row already carries the kept fragment; these are the co-components that
    would otherwise be lost. They share the source record, so the `variant` tells them apart in
    the name. No activity is copied onto them: the assay measured the parent record, not a salt.
    """
    from rdkit import Chem

    if not bool(getattr(batch.prefilter, "split_fragments", False)):
        return
    for fragment_index, fragment in extra_fragment_molecules(mol):
        variant = f"frag{fragment_index:02d}"
        fragment.SetProp("_Name", f"{name} [{variant}]")
        row = _build_row(
            source_file=batch.file_path,
            source_index=source_index,
            name=f"{name} [{variant}]",
            n_atoms=fragment.GetNumAtoms(),
            import_batch_id=batch.import_batch_id,
            stored=structure_payload(Chem.MolToMolBlock(fragment), "sdf"),
            current=None,
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
            prefilter=batch.prefilter,
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
