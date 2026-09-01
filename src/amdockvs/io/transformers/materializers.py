from __future__ import annotations

import io
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from amdockvs.chemistry.filtering import annotate_row_with_small_molecule_filter_values
from amdockvs.chemistry.state import molecule_state_metadata
from amdockvs.io.parsers import count_import_records, read_record_span
from amdockvs.io.payloads import ImportBatchPayload, ImportPrefilterPolicy, MultithreadedSDFImportPayload
from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    extract_component_to_pdb,
    scan_receptor_structure,
    write_processed_receptor,
)
from amdockvs.models import MoleculeRecord
from amdockvs.models.docking import BindingSite
from amdockvs.models.molecules import (
    ModelSource,
    MoleculeType,
    MoleculeUsageClass,
    sanitize_molecule_extra_data,
)
from amdockvs.vocab import BindingSiteSource, FileFormat, SetPurpose
from amdockvs.io.import_stats import (
    FILTERED_PREFILTER,
    FILTERED_PROPERTY,
    IMPORTED,
    NO_VALID_FRAGMENT,
    UNREADABLE,
    bump,
    write_import_stats,
)
from amdockvs.io.rows import (
    active_small_molecule_criteria,
    cull_mol,
    evaluate_ligand_mol,
    htp_mol_filter,
    ligand_row_fields,
    metadata_map_from_row,
    molecule_columns,
)
from amdockvs.molecules.fragments import write_fragment_files
from amdockvs.molecule_paths import artifact_storage_path, managed_paths_for_source


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
    molecule_sets: list[dict[str, Any]] = []
    molecule_set_members: list[dict[str, Any]] = []
    ligand_activities: list[dict[str, Any]] = []
    binding_sites: list[dict[str, Any]] = []
    seen_set_refs: set[str] = set()

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
        for set_spec in list(row.get("molecule_set_specs") or []):
            set_ref = str(set_spec.get("set_ref") or "").strip()
            existing_set_id = int(set_spec.get("existing_set_id") or 0)
            member_payload = {
                "molecule_ref": molecule_ref,
                "is_grid_reference": bool(set_spec.get("is_grid_reference")),
                "crystal_pose_path": str(set_spec.get("crystal_pose_path") or ""),
                "use_as_pharmacophore": bool(set_spec.get("use_as_pharmacophore")),
                "use_as_substructure": bool(set_spec.get("use_as_substructure")),
                "created_at": row.get("created_at"),
            }
            if existing_set_id > 0:
                member_payload["set_id"] = existing_set_id
                molecule_set_members.append(member_payload)
                continue
            if not set_ref:
                continue
            if set_ref not in seen_set_refs:
                molecule_sets.append(
                    {
                        "name": str(set_spec.get("name") or ""),
                        "purpose": str(set_spec.get("purpose") or SetPurpose.CUSTOM),
                        "description": str(set_spec.get("description") or ""),
                        "created_at": row.get("created_at"),
                        "$ref": set_ref,
                    }
                )
                seen_set_refs.add(set_ref)
            member_payload["set_ref"] = set_ref
            molecule_set_members.append(member_payload)
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
        "molecule_sets": molecule_sets,
        "molecule_set_members": molecule_set_members,
        "ligand_activities": ligand_activities,
        "binding_sites": binding_sites,
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
    usage_class: str = MoleculeUsageClass.GENERAL,
) -> dict[str, Any]:
    now = datetime.now()
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
        usage_class=str(usage_class or MoleculeUsageClass.GENERAL),
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
        "molecule_set_specs": [],
        "activity_spec": None,
        "binding_site_specs": [],
        "has_3d": bool(state.get("has_3d", bool(molecule_row.get("has_3d")))),
        "has_hs": bool(state.get("has_hs", bool(molecule_row.get("has_hs")))),
        "is_minimized": bool(state.get("is_minimized", bool(molecule_row.get("is_minimized")))),
        "conformer_count": max(0, int(state.get("conformer_count", int(molecule_row.get("conformer_count") or 0)) or 0)),
    }


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


def _progress_update(progress_cb: Callable[[float], None] | None, completed: int, total: int) -> None:
    if progress_cb is None or total <= 0:
        return
    progress_cb((max(0, completed) / max(1, total)) * 100.0)


def _active_binding_site_position(
    specs: list[dict[str, Any]],
    *,
    selected_source_ref: str = "",
    reference_ligands: list[str] | tuple[str, ...] | None = None,
) -> int | None:
    """Which of the receptor sites ends up active, by position in its own list.

    None of them has an id yet — they are inserted after the molecule — so the position is the
    only handle available. The sink turns it into a `$ref` and closes the FK with a final UPDATE.
    """
    if not specs:
        return None
    selected = str(selected_source_ref or "").strip()
    if not selected and reference_ligands is not None:
        refs = [str(value).strip() for value in reference_ligands if str(value).strip()]
        if len(refs) == 1:
            selected = refs[0]
    if selected:
        for position, item in enumerate(specs):
            if str(item.get("source_ref") or "") == selected:
                return position
    ligand_positions = [
        position
        for position, item in enumerate(specs)
        if str(item.get("source") or "").strip().lower() == "ligand"
    ]
    if len(ligand_positions) == 1:
        return ligand_positions[0]
    return None


def _load_small_molecule_from_path(path: Path):
    from rdkit import Chem

    suffix = path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        return Chem.MolFromMolFile(str(path), sanitize=True, removeHs=False)
    if suffix == ".mol2":
        return Chem.MolFromMol2File(str(path), sanitize=True, removeHs=False)
    if suffix == ".pdb":
        return Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=False)
    return None


def _receptor_import_options_from_patch(extra_data_patch: dict[str, Any]) -> ReceptorImportOptions:
    structure = dict(extra_data_patch.get("structure") or {})
    workflow = dict(extra_data_patch.get("workflow") or {})
    import_profile = dict(structure.get("import_profile") or {})
    return ReceptorImportOptions(
        use_biological_assembly=bool(import_profile.get("use_biological_assembly", True)),
        remove_non_structural_waters=bool(import_profile.get("remove_non_structural_waters", True)),
        create_binding_sites_from_components=bool(import_profile.get("create_binding_sites_from_components", False)),
        remove_cofactors=bool(import_profile.get("remove_cofactors", False)),
        remove_altloc=bool(import_profile.get("remove_altloc", True)),
        import_mode=str(workflow.get("import_mode") or "receptor"),
        binding_site_box_size=float(
            import_profile.get("binding_site_box_size")
            or workflow.get("binding_site_box_size")
            or ReceptorImportOptions().binding_site_box_size
        ),
        selected_cocrystal_key=str(workflow.get("selected_cocrystal_key") or ""),
        activity_text=str(workflow.get("activity") or "").strip(),
        selected_chain_ids=tuple(str(chain_id) for chain_id in (import_profile.get("selected_chain_ids") or ())),
        selected_assembly=str(import_profile.get("selected_assembly") or ""),
        selected_reference_ligands=(
            None
            if import_profile.get("selected_reference_ligands") is None
            else tuple(str(selector) for selector in import_profile.get("selected_reference_ligands") or ())
        ),
    )


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


def _cocrystal_ligand_specs(
    *,
    batch: ImportBatchPayload,
    source_file: Path,
    source_index: int,
    workflow: dict[str, Any],
    options: ReceptorImportOptions,
) -> list[dict[str, Any]]:
    # The user-selected cocrystal ligands become references (artifact copies were deselected in the
    # Ligands chips). Fall back to all candidates if an older patch has no explicit selection.
    chosen = workflow.get("reference_ligands")
    if chosen is None:
        chosen = workflow.get("ligand_candidates") or []
    selectors = [str(selector).strip() for selector in list(chosen) if str(selector).strip()]
    specs: list[dict[str, Any]] = []
    for offset, selector in enumerate(selectors, start=1):
        ligand_index = (int(source_index) * 1000) + offset
        ligand_paths = managed_paths_for_source(
            storage_root=batch.storage_dir,
            role="ligand",
            source_file=source_file,
            source_index=ligand_index,
            original_suffix=".pdb",
            current_suffix=".pdb",
        )
        extracted = extract_component_to_pdb(
            source_file,
            ligand_paths["original_path"],
            selector=selector,
            use_biological_assembly=False,  # extract the cocrystal pose as deposited (ASU coords)
        )
        if extracted is None:
            continue
        shutil.copy2(ligand_paths["original_path"], ligand_paths["current_path"])
        resname = str(extracted.get("resname") or "ligand").strip().upper()
        chain_id = str(extracted.get("chain_id") or "").strip()
        resseq = str(extracted.get("resseq") or "").strip()
        ligand_name = "_".join(part for part in (source_file.stem, resname, chain_id, resseq) if part)
        specs.append(
            {
                "name": ligand_name,
                "source_index": ligand_index,
                "stored_path": ligand_paths["original_path"],
                "current_path": ligand_paths["current_path"],
                "n_atoms": int(extracted.get("atom_count") or 0),
                "selector": selector,
                "center": tuple(extracted.get("center") or (None, None, None)),
            }
        )
    return specs


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
    project_root = _project_root_from_storage_dir(batch.storage_dir)
    for index, entry in enumerate(entries, start=1):
        source_index = int(entry.get("source_index") or 0)
        raw = entry.get("raw")
        if raw is not None:
            # Worker-side parse of the raw SDF record (the feed only sliced text).
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
        row = _build_row(
            project_root=project_root,
            source_file=batch.file_path,
            source_index=source_index,
            name=name,
            n_atoms=mol.GetNumAtoms(),
            input_format=FileFormat.SDF,
            stored_path=stored_path,
            current_path=current_path,
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
                _progress_update(progress_cb, index, total_entries)
                continue
        else:
            stored_path.write_text(mol_block, encoding="utf-8")
            current_path.write_text(mol_block, encoding="utf-8")
        bump(tally, IMPORTED)
        yield row
        _progress_update(progress_cb, index, total_entries)


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
        _progress_update(progress_cb, index, total_entries)


def _materialize_structure_rows(
    *,
    batch: ImportBatchPayload,
    entries: list[dict[str, Any]],
    progress_cb: Callable[[float], None] | None = None,
    tally: dict[str, int] | None = None,
) -> Iterable[dict[str, Any]]:
    tally = tally if tally is not None else {}
    resolved_entries = entries or [{"source_index": 0, "source_file": str(batch.file_path)}]
    total_entries = len(resolved_entries)
    project_root = _project_root_from_storage_dir(batch.storage_dir)
    for index, entry in enumerate(resolved_entries, start=1):
        source_index = int(entry.get("source_index") or 0)
        source_file = Path(entry.get("source_file") or batch.file_path).expanduser().resolve()
        suffix = source_file.suffix.lower() or ".dat"
        if (batch.primary_role or batch.kind) == "receptor":
            current_suffix = ".pdb"
        elif str(batch.primary_role or batch.kind).strip().lower() == "ligand" and str(batch.molecule_kind or "").strip().lower() == MoleculeType.SMALL_MOLECULE:
            current_suffix = ".sdf"
        else:
            current_suffix = suffix
        paths = managed_paths_for_source(
            storage_root=batch.storage_dir,
            role=batch.primary_role or batch.kind,
            source_file=source_file,
            source_index=source_index,
            original_suffix=suffix,
            current_suffix=current_suffix,
        )
        stored_path = paths["original_path"]
        current_path = paths["current_path"]
        shutil.copy2(source_file, stored_path)
        metadata = dict(batch.extra_data_patch or {})
        scan_payload = dict(metadata.pop("__scan", {}) or {})
        options = _receptor_import_options_from_patch(metadata) if (batch.primary_role or batch.kind) == "receptor" else None
        processing_summary: dict[str, Any] = {}
        if options is not None and not scan_payload:
            scan_payload = scan_receptor_structure(source_file)
        ligand_mol = None
        if options is not None and scan_payload:
            processing_summary = write_processed_receptor(
                source_file,
                current_path,
                scan=scan_payload,
                options=options,
            )
        else:
            ligand_role = str(batch.primary_role or batch.kind).strip().lower() == "ligand"
            if ligand_role and str(batch.molecule_kind or "").strip().lower() == MoleculeType.SMALL_MOLECULE:
                ligand_mol = _load_small_molecule_from_path(source_file)
            if ligand_mol is not None:
                current_path.write_text("", encoding="utf-8")
            else:
                shutil.copy2(source_file, current_path)
        metadata = {
            **metadata,
            "processing": processing_summary,
        }
        metadata["state"] = {
            "has_3d": current_suffix in {".pdb", ".pdbqt", ".mol2"},
            "has_hs": current_suffix == ".pdbqt",
            "conformer_count": 1 if current_suffix in {".pdb", ".pdbqt", ".mol2"} else 0,
        }
        row = _build_row(
            project_root=project_root,
            source_file=source_file,
            source_index=source_index,
            name=source_file.stem,
            n_atoms=_count_atoms_from_structure_lines(current_path),
            input_format=suffix.lstrip("."),
            stored_path=stored_path,
            current_path=current_path,
            metadata=metadata,
            molecule_kind=batch.molecule_kind,
            primary_role=batch.primary_role,
            primary_context=batch.primary_context,
        )
        if ligand_mol is not None:
            row, reason = _finalize_ligand_row_from_mol(
                row=row,
                mol=ligand_mol,
                storage_root=batch.storage_dir,
                role=batch.primary_role or batch.kind,
                storage_key=str(paths["key"]),
                project_root=project_root,
                current_path=current_path,
                prefilter=batch.prefilter,
            )
            if reason is not None:
                bump(tally, reason)
                _progress_update(progress_cb, index, total_entries)
                continue
        row["binding_site_specs"] = [
            {
                "name": str(item.get("name") or ""),
                "source": str(item.get("source") or BindingSiteSource.MANUAL),
                "source_ref": str(item.get("source_ref") or ""),
                "center": tuple(item.get("center") or (None, None, None)),
                "size": tuple(item.get("size") or (None, None, None)),
                "extra_data": dict(item.get("extra_data") or {}),
            }
            for item in list(batch.binding_site_specs or [])
        ]
        workflow = dict(metadata.get("workflow") or {})
        active_position = _active_binding_site_position(
            row["binding_site_specs"],
            selected_source_ref=str(workflow.get("selected_cocrystal_key") or "").strip(),
            reference_ligands=workflow.get("reference_ligands"),
        )
        if active_position is not None:
            row["active_binding_site_position"] = active_position
        bump(tally, IMPORTED)
        yield row
        if options is not None:
            workflow = dict(metadata.get("workflow") or {})
            ligand_specs_list = _cocrystal_ligand_specs(
                batch=batch,
                source_file=source_file,
                source_index=source_index,
                workflow=workflow,
                options=options,
            )
            reference_receptor_path = ""
            if ligand_specs_list:
                reference_path = artifact_storage_path(
                    batch.storage_dir,
                    role="receptor",
                    key=str(paths["key"]),
                    artifact_name="reference",
                    suffix=".pdb",
                )
                shutil.copy2(current_path, reference_path)
                reference_receptor_path = str(reference_path.relative_to(project_root))
            for ligand_specs in ligand_specs_list:
                # Freeze the native ligand pose alongside the receptor snapshot so redocking
                # RMSD has a stable reference that prep/3D regen can't move later.
                reference_ligand_path = ""
                ligand_current = Path(ligand_specs["current_path"])
                if ligand_current.exists():
                    ligand_reference_path = artifact_storage_path(
                        batch.storage_dir,
                        role="ligand",
                        key=str(ligand_specs["selector"] or paths["key"]),
                        artifact_name="reference",
                        suffix=ligand_current.suffix or ".pdb",
                    )
                    shutil.copy2(ligand_current, ligand_reference_path)
                    reference_ligand_path = str(ligand_reference_path.relative_to(project_root))
                ligand_row = _build_row(
                    project_root=project_root,
                    source_file=source_file,
                    source_index=int(ligand_specs["source_index"]),
                    name=str(ligand_specs["name"] or f"{source_file.stem}_ligand"),
                    n_atoms=int(ligand_specs["n_atoms"] or 0),
                    input_format="pdb",
                    stored_path=Path(ligand_specs["stored_path"]),
                    current_path=Path(ligand_specs["current_path"]),
                    metadata={
                        "state": {"has_3d": True, "has_hs": False, "conformer_count": 1},
                        "workflow_origin": {
                            "receptor_source": str(source_file),
                            "selected_cocrystal_key": str(ligand_specs["selector"] or ""),
                        },
                    },
                    molecule_kind=MoleculeType.SMALL_MOLECULE,
                    primary_role="ligand",
                    primary_context="cocrystal",
                    usage_class=MoleculeUsageClass.REFERENCE,
                )
                ligand_row["complex_spec"] = {
                    "name": f"{source_file.stem}_{ligand_specs['selector']}",
                    "receptor_ref": f"molecule::{source_file}::{source_index}",
                    "reference_receptor_path": reference_receptor_path,
                    "reference_ligand_path": reference_ligand_path,
                    "binding_site_position": row.get("active_binding_site_position"),
                    "purpose": "reference",
                    "metadata": {
                        "selected_cocrystal_key": str(ligand_specs.get("selector") or ""),
                    },
                }
                yield ligand_row
        _progress_update(progress_cb, index, total_entries)


def _count_atoms_from_structure_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("ATOM") or line.startswith("HETATM"):
                count += 1
    return count


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


__all__ = [
    "build_import_graph_payload",
    "materialize_import_batch",
    "materialize_multithreaded_sdf_file",
]
