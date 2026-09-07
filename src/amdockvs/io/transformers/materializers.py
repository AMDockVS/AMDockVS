"""Import entry points: a batch of files in, molecule rows out.

Dispatch only — the per-format work lives in `sdf`, `smiles` and `structures`, and the
row shape they all produce in `rows`. `build_import_graph_payload` turns those rows into
the graph the import job persists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.parsers import count_import_records, read_record_span
from amdockvs.io.payloads import ImportBatchPayload, MultithreadedSDFImportPayload
from amdockvs.models.molecules import ModelSource, MoleculeType, MoleculeUsageClass
from amdockvs.core.vocab import BindingSiteSource, FileFormat
from amdockvs.io.import_stats import FILTERED_PREFILTER, IMPORTED, UNREADABLE, bump, write_import_stats
from amdockvs.io.rows import active_small_molecule_criteria, cull_mol, htp_mol_filter, metadata_map_from_row
from amdockvs.core.paths import managed_paths_for_source
from amdockvs.io.transformers.rows import (
    _apply_sdf_activity,
    _build_row,
    _finalize_ligand_row_from_mol,
    _progress_update,
    _project_root_from_storage_dir,
    _source_properties_from_mapping,
)
from amdockvs.io.transformers.sdf import _materialize_sdf_rows, _mol_source_properties
from amdockvs.io.transformers.smiles import _materialize_smiles_rows
from amdockvs.io.transformers.structures import _materialize_structure_rows


def materialize_import_batch(
    payload: dict[str, Any],
    progress_cb: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Convert one import batch into normalized molecule rows."""
    batch = ImportBatchPayload.model_validate(payload)

    if not batch.file_path.exists() or not batch.file_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {batch.file_path}")
    batch.storage_dir.mkdir(parents=True, exist_ok=True)

    # Current path: the chunk carries a byte range and its records are re-read here. `records`
    # (embedded raw text) still works for formats without a separator and for old payloads;
    # `entries` is the legacy pre-parsed shape.
    if batch.span_count > 0 and batch.span_offset >= 0:
        records = read_record_span(
            file_path=batch.file_path,
            input_format=batch.input_format,
            offset=batch.span_offset,
            end=batch.span_end,
            first_index=batch.span_first_index,
        )
        if len(records) != batch.span_count:
            raise ValueError(
                f"Range {batch.span_offset}..{batch.span_end} of {batch.file_path} yields "
                f"{len(records)} records, not the {batch.span_count} the feed declared: "
                "the file changed since the job was created."
            )
    else:
        records = batch.records if batch.records else batch.entries
    tally: dict[str, int] = {}
    if batch.input_format == FileFormat.SDF:
        rows = list(_materialize_sdf_rows(batch=batch, entries=records, progress_cb=progress_cb, tally=tally))
    elif batch.input_format == FileFormat.SMILES:
        rows = list(_materialize_smiles_rows(batch=batch, entries=records, progress_cb=progress_cb, tally=tally))
    else:
        rows = list(_materialize_structure_rows(batch=batch, entries=records, progress_cb=progress_cb, tally=tally))
    write_import_stats(batch.storage_dir, tally)
    return rows

def materialize_multithreaded_sdf_file(
    payload: dict[str, Any],
    progress_cb: Callable[[float], None] | None = None,
) -> list[dict[str, Any]]:
    """Parse one full SDF file with RDKit's multithreaded supplier."""
    from rdkit import Chem

    batch = MultithreadedSDFImportPayload.model_validate(payload)
    if batch.file_path.suffix.lower() != ".sdf":
        raise ValueError("materialize_multithreaded_sdf_file only supports .sdf files.")
    if not batch.file_path.exists() or not batch.file_path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {batch.file_path}")

    expected_records = max(1, count_import_records(batch.file_path))
    batch.storage_dir.mkdir(parents=True, exist_ok=True)
    project_root = _project_root_from_storage_dir(batch.storage_dir)
    supplier = Chem.MultithreadedSDMolSupplier(
        str(batch.file_path),
        sanitize=True,
        removeHs=False,
        strictParsing=True,
        numWriterThreads=batch.num_threads,
    )

    criteria = active_small_molecule_criteria(batch.prefilter, batch.molecule_kind)
    htp_passes = htp_mol_filter(batch.prefilter, batch.molecule_kind)
    tally: dict[str, int] = {}
    rows_by_index: dict[int, dict[str, Any]] = {}
    captured = 0
    processed = 0
    for fallback_index, mol in enumerate(supplier):
        processed += 1
        record_id = int(getattr(supplier, "GetLastRecordId")() or 0)
        source_index = max(0, (record_id - 1) if record_id > 0 else fallback_index)
        if mol is None:
            bump(tally, UNREADABLE)
            captured = min(expected_records, max(captured, processed))
            _progress_update(progress_cb, captured, expected_records)
            continue

        # HTP pre-materialization cull: filter on the in-memory mol before writing any file.
        if htp_passes is not None and not htp_passes(cull_mol(mol, batch)):
            bump(tally, FILTERED_PREFILTER)
            captured = min(expected_records, processed)
            _progress_update(progress_cb, captured, expected_records)
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
        mol_block = Chem.MolToMolBlock(mol)
        name = mol.GetProp("_Name") if mol.HasProp("_Name") else f"{batch.file_path.stem}_{source_index}"
        row = _build_row(
            project_root=project_root,
            source_file=batch.file_path,
            source_index=source_index,
            name=name,
            n_atoms=mol.GetNumHeavyAtoms(),
            input_format=FileFormat.SDF,
            stored_path=stored_path,
            current_path=current_path,
            metadata={
                **molecule_state_metadata(mol),
                "reader": "MultithreadedSDMolSupplier",
                "num_threads": batch.num_threads,
            },
            molecule_kind=batch.molecule_kind,
            primary_role=batch.primary_role,
            primary_context=batch.primary_context,
        )
        row["source_properties"] = _source_properties_from_mapping(_mol_source_properties(mol))
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
                mol_block=mol_block,
                criteria=criteria,
                molecule_kind=batch.molecule_kind,
            )
            if reason is not None:
                bump(tally, reason)
                captured = min(expected_records, processed)
                _progress_update(progress_cb, captured, expected_records)
                continue
        else:
            stored_path.write_text(mol_block, encoding="utf-8")
            current_path.write_text(mol_block, encoding="utf-8")
        bump(tally, IMPORTED)
        rows_by_index[source_index] = row
        captured = min(expected_records, processed)
        _progress_update(progress_cb, captured, expected_records)
        if captured >= expected_records:
            break

    write_import_stats(batch.storage_dir, tally)
    return [row for _, row in sorted(rows_by_index.items(), key=lambda item: item[0])]

def offload_source_properties(rows: list[dict[str, Any]], storage_dir: Any) -> str | None:
    """Move the heavy per-molecule SDF tags out of the graph payload into a parquet
    sidecar, load-on-demand. source_properties are ~34 rows/mol (~97% of all import
    rows) and are almost never viewed and never used hot — persisting them into the
    project DB is what makes the single sqlite writer the import bottleneck.

    Each row's source_properties are emptied (so build_import_graph_payload emits no
    molecule_source_properties node) and the row records a '__props_shard' ref in its
    extra_data so its tags can be reloaded by (shard, source_index). Returns the shard
    filename, or None if there were no properties.
    """
    from uuid import uuid4

    from amdockvs.io.properties import PROPERTIES_SUBDIR, PROPS_SHARD_KEY

    src_idx: list[int] = []
    keys: list[str] = []
    vals: list[str] = []
    for row in rows:
        source_index = int(row.get("source_index") or 0)
        for prop in list(row.get("source_properties") or []):
            key = str(prop.get("key") or "").strip()
            value = str(prop.get("value_text") or "").strip()
            if not key or not value:
                continue
            src_idx.append(source_index)
            keys.append(key)
            vals.append(value)
    if not src_idx:
        return None

    import pyarrow as pa
    import pyarrow.parquet as pq

    props_dir = Path(storage_dir).expanduser().resolve() / PROPERTIES_SUBDIR
    props_dir.mkdir(parents=True, exist_ok=True)
    shard = f"{uuid4().hex}.parquet"
    pq.write_table(
        pa.table({"source_index": src_idx, "key": keys, "value": vals}),
        props_dir / shard,
        compression="zstd",
    )
    for row in rows:
        row["source_properties"] = []
        extra = row.get("extra_data")
        extra = dict(extra) if isinstance(extra, dict) else {}
        extra[PROPS_SHARD_KEY] = shard
        row["extra_data"] = extra
    return shard

def _site_ref(molecule_ref: str, position: Any) -> str | None:
    return None if position is None else f"{molecule_ref}::site::{int(position)}"

def build_import_graph_payload(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    molecules: list[dict[str, Any]] = []
    molecule_models: list[dict[str, Any]] = []
    molecule_source_properties: list[dict[str, Any]] = []
    complexes: list[dict[str, Any]] = []
    ligand_activities: list[dict[str, Any]] = []
    binding_sites: list[dict[str, Any]] = []
    engine_states: list[dict[str, Any]] = []

    for row in rows:
        source = str(row.get("source") or "")
        source_index = int(row.get("source_index") or 0)
        molecule_ref = f"molecule::{source}::{source_index}"
        current_model_index: int | None = None
        role = str(row.get("primary_role") or "").strip().lower()
        metadata_map = metadata_map_from_row(row)
        if bool(row.get("has_3d")) and str(row.get("current_path") or "").strip():
            current_model_index = 0
            molecule_models.append(
                {
                    "molecule_ref": molecule_ref,
                    "model_index": 0,
                    "file_path": str(row.get("current_path") or ""),
                    "energy": None,
                    "source": ModelSource.IMPORTED,
                    "created_at": row.get("created_at"),
                }
            )
        molecule_payload = {
            "name": str(row.get("name") or ""),
            "molecule_type": str(row.get("molecule_kind") or "unknown"),
            "source": source,
            "source_index": source_index,
            "input_format": str(row.get("input_format") or ""),
            "is_receptor": role == "receptor",
            "is_ligand": role == "ligand",
            "active_binding_site_id": None,
            "active_binding_site_ref": _site_ref(molecule_ref, row.get("active_binding_site_position")),
            "stored_path": str(row.get("stored_path") or ""),
            "current_path": str(row.get("current_path") or ""),
            "current_model_index": current_model_index,
            "n_atoms": int(row.get("n_atoms") or 0),
            "mw": row.get("mw"),
            "exact_mw": row.get("exact_mw"),
            "logp": row.get("logp"),
            "hbd": row.get("hbd"),
            "hba": row.get("hba"),
            "tpsa": row.get("tpsa"),
            "rotatable_bonds": row.get("rotatable_bonds"),
            "fragment_count": row.get("fragment_count"),
            "ring_count": row.get("ring_count"),
            "aromatic_ring_count": row.get("aromatic_ring_count"),
            "hetero_atom_count": row.get("hetero_atom_count"),
            "heavy_atom_count": row.get("heavy_atom_count"),
            "formal_charge": row.get("formal_charge"),
            "fraction_csp3": row.get("fraction_csp3"),
            "pains_matches": list(row.get("pains_matches") or []),
            "ro5_violations": list(row.get("ro5_violations") or []),
            "conformer_count": int(row.get("conformer_count") or 0),
            "has_3d": bool(row.get("has_3d")),
            "has_hs": bool(row.get("has_hs")),
            "is_minimized": bool(row.get("is_minimized")),
            "has_activity": bool(row.get("has_activity")),
            "excluded": bool(row.get("excluded")),
            "exclusion_reason": str(row.get("exclusion_reason") or ""),
            "in_set": bool(row.get("in_set")),
            "usage_class": str(row.get("usage_class") or MoleculeUsageClass.GENERAL),
            "primary_context": str(row.get("primary_context") or ""),
            "extra_data": metadata_map,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "$ref": molecule_ref,
        }
        molecules.append(molecule_payload)
        for source_prop in list(row.get("source_properties") or []):
            key = str(source_prop.get("key") or "").strip()
            value_text = str(source_prop.get("value_text") or "").strip()
            if not key or not value_text:
                continue
            molecule_source_properties.append(
                {
                    "molecule_ref": molecule_ref,
                    "key": key,
                    "value_text": value_text,
                }
            )
        # One 'activity_spec' (single-endpoint / redocking-complex path) plus any 'activity_specs'
        # (multi-column import, e.g. Tox21's 12 assays) — each becomes its own ActivityRecord.
        activity_specs: list[dict[str, Any]] = []
        single_spec = row.get("activity_spec")
        if isinstance(single_spec, dict) and single_spec.get("value") is not None:
            activity_specs.append(single_spec)
        for extra in (row.get("activity_specs") or []):
            if isinstance(extra, dict) and extra.get("value") is not None:
                activity_specs.append(extra)
        activity_ref: str | None = None
        for spec_index, spec in enumerate(activity_specs):
            spec_ref = f"activity::{source}::{source_index}::{spec_index}"
            if activity_ref is None:
                activity_ref = spec_ref  # a redocking complex links to the first activity
            ligand_activities.append(
                {
                    "$ref": spec_ref,
                    "molecule_ref": molecule_ref,
                    "value": spec.get("value"),
                    "unit": str(spec.get("unit") or ""),
                    "activity_type": str(spec.get("activity_type") or ""),
                    "kind": str(spec.get("kind") or "continuous"),
                    "description": str(spec.get("description") or ""),
                    "source": str(spec.get("source") or ""),
                    "created_at": row.get("created_at"),
                }
            )
        complex_spec = row.get("complex_spec") if isinstance(row.get("complex_spec"), dict) else {}
        if complex_spec:
            complexes.append(
                {
                    "name": str(complex_spec.get("name") or ""),
                    "receptor_ref": str(complex_spec.get("receptor_ref") or ""),
                    "ligand_ref": molecule_ref,
                    "reference_receptor_path": str(complex_spec.get("reference_receptor_path") or ""),
                    "reference_ligand_path": str(complex_spec.get("reference_ligand_path") or ""),
                    # The site belongs to the receptor, not to the ligand whose row carries complex_spec.
                    "binding_site_ref": _site_ref(
                        str(complex_spec.get("receptor_ref") or ""),
                        complex_spec.get("binding_site_position"),
                    ),
                    "activity_ref": activity_ref,
                    "purpose": str(complex_spec.get("purpose") or "redocking"),
                    "metadata_json": json.dumps(dict(complex_spec.get("metadata") or {}), ensure_ascii=True),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        for engine_spec in list(row.get("engine_state_specs") or []):
            engine_states.append(
                {
                    "molecule_ref": molecule_ref,
                    "role_type": str(engine_spec.get("role_type") or role),
                    "engine": str(engine_spec.get("engine") or "ad4"),
                    "files": dict(engine_spec.get("files") or {}),
                    "is_ready": bool(engine_spec.get("is_ready")),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        for position, site_spec in enumerate(list(row.get("binding_site_specs") or [])):
            center = tuple(site_spec.get("center") or (None, None, None))
            size = tuple(site_spec.get("size") or (None, None, None))
            binding_sites.append(
                {
                    "$ref": _site_ref(molecule_ref, position),
                    "molecule_ref": molecule_ref,
                    "name": str(site_spec.get("name") or ""),
                    "source": str(site_spec.get("source") or BindingSiteSource.MANUAL),
                    "source_ref": str(site_spec.get("source_ref") or ""),
                    "center_x": center[0],
                    "center_y": center[1],
                    "center_z": center[2],
                    "size_x": size[0],
                    "size_y": size[1],
                    "size_z": size[2],
                    "extra_data": dict(site_spec.get("extra_data") or {}),
                    "created_at": row.get("created_at"),
                }
            )

    return {
        "molecules": molecules,
        "molecule_models": molecule_models,
        "molecule_source_properties": molecule_source_properties,
        "complexes": complexes,
        "ligand_activities": ligand_activities,
        "binding_sites": binding_sites,
        "engine_states": engine_states,
    }


__all__ = [
    "build_import_graph_payload",
    "materialize_import_batch",
    "materialize_multithreaded_sdf_file",
]
