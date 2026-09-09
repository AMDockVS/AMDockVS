"""Materializing a PDB/CIF structure: the receptor, its cofactors and its co-crystal ligands."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from amdockvs.io.payloads import ImportBatchPayload
from amdockvs.io.receptor_preview import (
    ReceptorImportOptions,
    extract_component_to_pdb,
    scan_receptor_structure,
    write_processed_receptor,
)
from amdockvs.models.molecules import MoleculeType, MoleculeUsageClass
from amdockvs.core.vocab import BindingSiteSource
from amdockvs.io.import_stats import IMPORTED, bump
from amdockvs.io.formats import as_pdb, canonical_suffix
from amdockvs.core.paths import artifact_storage_path, managed_paths_for_source
from amdockvs.io.transformers.rows import (
    _build_row,
    _finalize_ligand_row_from_mol,
    _progress_update,
    _project_root_from_storage_dir,
)


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
            current_suffix = canonical_suffix(batch.molecule_kind or MoleculeType.PROTEIN)
        elif str(batch.primary_role or batch.kind).strip().lower() == "ligand" and str(batch.molecule_kind or "").strip().lower() == MoleculeType.SMALL_MOLECULE:
            current_suffix = ".sdf"
        elif suffix == ".pdbqt":
            current_suffix = ".pdb"
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
        ligand_mol = None
        # A PDBQT is an engine artifact, not a structure format: hand the scanners a PDB.
        with as_pdb(source_file) as structure_source:
            if options is not None and not scan_payload:
                scan_payload = scan_receptor_structure(structure_source)
            if options is not None and scan_payload:
                processing_summary = write_processed_receptor(
                    structure_source,
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
                    shutil.copy2(structure_source, current_path)
        metadata = {
            **metadata,
            "processing": processing_summary,
        }
        # Every structure format here carries coordinates; only SMILES-like inputs do not.
        has_3d = current_suffix in {".pdb", ".pdbqt", ".mol2", ".cif", ".sdf"}
        metadata["state"] = {
            "has_3d": has_3d,
            "has_hs": ".pdbqt" in {current_suffix, suffix},
            "conformer_count": 1 if has_3d else 0,
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
        if suffix == ".pdbqt":
            # The imported file already is what preparation would have produced: register it
            # as the ad4 artifact so the molecule is dockable without re-preparing it.
            row["engine_state_specs"] = [
                {
                    "role_type": str(batch.primary_role or batch.kind).strip().lower(),
                    "engine": "ad4",
                    "files": {"prepared": str(stored_path)},
                    "is_ready": True,
                }
            ]
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
                    suffix=current_path.suffix or ".pdb",
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
                # This row skips _finalize_ligand_row_from_mol (the receptor route has no ligand
                # Mol), so without this its descriptors stay NULL and any table filter on them
                # drops the reference ligand silently. A .cif source gives all of them (deposited
                # bond orders); a .pdb source gives only PDB_SAFE_DESCRIPTORS.
                ligand_row.update(
                    cocrystal_ligand_descriptors(
                        ligand_current,
                        source_file=source_file,
                        resname=str(ligand_specs["selector"] or "").split(":")[0],
                    )
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

# CONECT records, so RDKit perceives connectivity by proximity and every bond comes out single.
# Measured by round-tripping 6 drug-like molecules through a CONECT-less PDB
# (test/test_cocrystal_descriptors.py): these five match the true value 6/6, while
# mw/exact_mw/logp/tpsa/hbd/aromatic_ring_count/fraction_csp3 match 0/6.
# ponytail: the rest stays NULL — a wrong MW is worse than no MW once a table filter reads it.
# This is the last resort: cocrystal_ligand_descriptors tries the CCD first and only lands here for
# a residue name the CCD does not know (a ligand drawn by a tool, named UNL/LIG).
PDB_SAFE_DESCRIPTORS = (
    "fragment_count",
    "ring_count",
    "hetero_atom_count",
    "heavy_atom_count",
    "formal_charge",
)


def cocrystal_ligand_descriptors(
    path: Path, *, source_file: Path | None = None, resname: str = ""
) -> dict[str, Any]:
    """Descriptors for an extracted cocrystal ligand: all of them from a CIF, five from a PDB.

    Bond orders come from the entry mmCIF if the receptor arrived as one, else from the CCD
    component for ``resname`` (cached, ~10 KB). Only if both miss does this fall back to
    PDB_SAFE_DESCRIPTORS, because a PDB alone cannot say.
    """
    from amdockvs.chemistry.descriptors import calculate_basic_descriptors
    from amdockvs.io.ccd_bonds import ccd_component_file, ligand_from_cif

    if not path.exists():
        return {}
    for lookup in (lambda: source_file, lambda: ccd_component_file(resname)):
        cif = lookup()
        if cif is None:
            continue
        mol = ligand_from_cif(cif, path, resname)
        if mol is not None:
            return calculate_basic_descriptors(mol)
    try:
        mol = _load_small_molecule_from_path(path)
    except (OSError, ValueError):  # unreadable or malformed — descriptors are not worth an abort
        return {}
    if mol is None:
        return {}
    values = calculate_basic_descriptors(mol)
    return {key: values[key] for key in PDB_SAFE_DESCRIPTORS if key in values}

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
    from amdockvs.io.formats import read_mol

    return read_mol(path)
