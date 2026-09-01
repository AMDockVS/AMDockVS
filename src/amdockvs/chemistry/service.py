from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from amdockvs.chemistry.conformers import generate_conformer_ensemble
from amdockvs.chemistry.protonation import protonate_molecule_batch
from amdockvs.chemistry.tools import (
    fix_receptor_pdb_file,
    generate_ligand_3d,
    minimize_ligand_molecule,
    minimize_receptor_openmm_file,
    protonate_receptor_pdb2pqr_file,
    protonate_receptor_reduce_file,
    standardize_ligand_molecule,
)
from amdockvs.models.molecules import ModelSource, MoleculeModel
from amdockvs.molecule_paths import (
    artifact_path_for_existing,
    current_molecule_path,
    set_default_project_root,
    stored_molecule_path,
)


def decode_metadata(raw_metadata: Any) -> dict[str, Any]:
    if raw_metadata is None:
        return {}
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(str(raw_metadata))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def chemistry_current_entry(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    del metadata
    return None


def ligand_working_path(row: Mapping[str, Any], *, structure_source: str = "current") -> Path:
    source = str(structure_source or "current").strip().lower()
    path = stored_molecule_path(row) if source == "original" else current_molecule_path(row)
    if path is None:
        raise ValueError(f"Ligand {row.get('id')} has no stored_path.")
    return path


def receptor_working_path(row: Mapping[str, Any], *, structure_source: str = "current") -> Path:
    source = str(structure_source or "current").strip().lower()
    path = stored_molecule_path(row) if source == "original" else current_molecule_path(row)
    if path is None:
        raise ValueError(f"Receptor {row.get('id')} has no stored_path.")
    return path


def merge_chemistry_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    operation: str,
    path: Path,
    source_path: Path,
    params: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    promote_current: bool = True,
) -> dict[str, Any]:
    del operation, path, source_path, params, state, promote_current
    merged = decode_metadata(metadata)
    merged.pop("chemistry", None)
    merged.pop("history", None)
    merged.pop("project_root", None)
    merged.pop("state", None)
    return merged


def _project_root_from_row(row: Mapping[str, Any], *, output_dir: Path) -> Path:
    del row
    return output_dir.expanduser().resolve().parent.parent


def _relative_to_project_root(path: Path, *, project_root: Path) -> str:
    return str(path.expanduser().resolve().relative_to(project_root.expanduser().resolve()))


def _load_ligand_mol(path: Path):
    from rdkit import Chem

    suffix = path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        supplier = Chem.SDMolSupplier(str(path), sanitize=True, removeHs=False)
        mol = supplier[0] if supplier and len(supplier) > 0 else None
    elif suffix == ".mol2":
        mol = Chem.MolFromMol2File(str(path), sanitize=True, removeHs=False)
    elif suffix == ".pdb":
        mol = Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=False)
    else:
        raise ValueError(f"Chemistry ligand tools do not support format '{suffix}'.")
    if mol is None:
        raise ValueError(f"RDKit could not parse ligand file: {path}")
    return mol


def _write_ligand_mol(mol, path: Path, *, conf_id: int | None = None) -> None:
    from rdkit import Chem

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Chem.MolToMolBlock(mol, confId=conf_id if conf_id is not None else -1), encoding="utf-8")


def _write_ligand_conformer_files(
    mol,
    *,
    output_dir: Path,
    source_path: Path,
    start_index: int,
    project_root: Path,
) -> tuple[list[dict[str, Any]], str | None, int | None]:
    model_rows: list[dict[str, Any]] = []
    active_relative_path: str | None = None
    active_model_index: int | None = None
    for offset, conformer in enumerate(mol.GetConformers()):
        model_index = int(start_index) + int(offset)
        output_path = artifact_path_for_existing(
            output_dir,
            role="ligand",
            source_path=source_path,
            artifact_name=f"conformer_{model_index}",
            suffix=".sdf",
        )
        _write_ligand_mol(mol, output_path, conf_id=conformer.GetId())
        relative_path = _relative_to_project_root(output_path, project_root=project_root)
        if active_relative_path is None:
            active_relative_path = relative_path
            active_model_index = model_index
        model_rows.append(
            MoleculeModel.build_row(
                molecule_id=0,
                model_index=model_index,
                file_path=relative_path,
                source=ModelSource.ETKDG,
                energy=None,
            )
        )
    return model_rows, active_relative_path, active_model_index


def transform_ligand_rows(
    *,
    operation: str,
    output_dir: Path,
    rows: Iterable[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    next_model_index_by_entity: Mapping[int, int] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    operation_name = str(operation or "").strip().lower()
    if operation_name not in {"standardize", "protonate", "generate_3d", "conformers", "minimize"}:
        raise ValueError(f"Unsupported ligand chemistry operation: {operation}")

    normalized_params = dict(params or {})
    project_root = output_dir.expanduser().resolve().parent.parent
    set_default_project_root(project_root)
    next_index_map = {int(key): int(value) for key, value in dict(next_model_index_by_entity or {}).items()}
    row_list = [dict(row) for row in rows]
    updates: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    structure_source = str(normalized_params.get("structure_source") or "current")
    run_id = str(normalized_params.get("run_id") or "run")[:16]
    protonated_by_id: dict[int, Any] = {}
    loaded_protonation_inputs: dict[int, tuple[Path, Any]] = {}
    if operation_name == "protonate":
        for row in row_list:
            ligand_id = int(row.get("id") or 0)
            if ligand_id <= 0:
                continue
            try:
                source_path = ligand_working_path(row, structure_source=structure_source)
                loaded_protonation_inputs[ligand_id] = (source_path, _load_ligand_mol(source_path))
            except Exception as exc:
                failed_rows.append({"entity_id": ligand_id, "source_path": "", "error": str(exc)})
        protonated_by_id = protonate_molecule_batch(
            [(entity_id, item[1]) for entity_id, item in loaded_protonation_inputs.items()],
            method=str(normalized_params.get("method") or "dimorphite"),
            params=normalized_params,
        )
    for index, row in enumerate(row_list, start=1):
        ligand_id = int(row.get("id") or 0)
        if ligand_id <= 0:
            continue
        if operation_name == "protonate" and ligand_id not in loaded_protonation_inputs:
            if progress_cb is not None:
                progress_cb((index / max(1, len(row_list))) * 100.0)
            continue
        try:
            source_path = ligand_working_path(row, structure_source=structure_source)
            metadata = decode_metadata(row.get("extra_data"))
            mol = loaded_protonation_inputs[ligand_id][1] if operation_name == "protonate" else _load_ligand_mol(source_path)
            model_rows: list[dict[str, Any]] = []
            current_relative_path: str | None = None
            current_model_index = row.get("current_model_index")
            next_index = int(next_index_map.get(ligand_id, 0))

            if operation_name == "standardize":
                result_mol = standardize_ligand_molecule(
                    mol,
                    fragment_parent=bool(normalized_params.get("fragment_parent", True)),
                    fragment_mode=normalized_params.get("fragment_mode"),
                    neutralize=bool(normalized_params.get("neutralize", True)),
                    canonicalize_tautomer=bool(normalized_params.get("canonicalize_tautomer", False)),
                )
                output_path = artifact_path_for_existing(
                    output_dir,
                    role="ligand",
                    source_path=source_path,
                    artifact_name=f"standardized_{run_id}",
                    suffix=".sdf",
                )
                _write_ligand_mol(result_mol, output_path)
                current_relative_path = _relative_to_project_root(output_path, project_root=project_root)
                current_model_index = current_model_index if result_mol.GetNumConformers() > 0 else None
                state = {"has_hs": False, "has_3d": result_mol.GetNumConformers() > 0, "is_minimized": False}
            elif operation_name == "protonate":
                result_mol = protonated_by_id.get(ligand_id)
                if result_mol is None:
                    raise ValueError("The selected protonation method returned no structure.")
                method = str(normalized_params.get("method") or "dimorphite")
                output_path = artifact_path_for_existing(
                    output_dir,
                    role="ligand",
                    source_path=source_path,
                    artifact_name=f"protonated_{method}_{run_id}",
                    suffix=".sdf",
                )
                _write_ligand_mol(result_mol, output_path)
                current_relative_path = _relative_to_project_root(output_path, project_root=project_root)
                current_model_index = current_model_index if result_mol.GetNumConformers() > 0 else None
                state = {"has_hs": True, "has_3d": result_mol.GetNumConformers() > 0, "is_minimized": False}
            elif operation_name == "generate_3d":
                result_mol = generate_ligand_3d(
                    mol,
                    add_hs=bool(normalized_params.get("add_hs", True)),
                    random_seed=int(normalized_params.get("random_seed", 0xF00D)),
                    optimize=bool(normalized_params.get("optimize", True)),
                    fragment_mode=str(normalized_params.get("fragment_mode") or "largest_organic"),
                    filter_metals=bool(normalized_params.get("filter_metals", True)),
                    filter_simple_ions=bool(normalized_params.get("filter_simple_ions", True)),
                )
                current_model_index = next_index
                output_path = artifact_path_for_existing(
                    output_dir,
                    role="ligand",
                    source_path=source_path,
                    artifact_name=f"model_{current_model_index}_{run_id}",
                    suffix=".sdf",
                )
                _write_ligand_mol(result_mol, output_path)
                current_relative_path = _relative_to_project_root(output_path, project_root=project_root)
                generated_is_minimized = (
                    result_mol.GetBoolProp("_amdock_is_minimized")
                    if result_mol.HasProp("_amdock_is_minimized")
                    else False
                )
                model_rows.append(
                    {
                        **MoleculeModel.build_row(
                            molecule_id=ligand_id,
                            model_index=int(current_model_index),
                            file_path=current_relative_path,
                            source=ModelSource.RDKIT,
                            energy=None,
                        )
                    }
                )
                state = {
                    "has_hs": bool(normalized_params.get("add_hs", True)),
                    "has_3d": result_mol.GetNumConformers() > 0,
                    "is_minimized": bool(generated_is_minimized),
                }
            elif operation_name == "conformers":
                result_mol, _conf_ids = generate_conformer_ensemble(
                    mol,
                    num_conformers=int(normalized_params.get("num_conformers", 20)),
                    add_hs=bool(normalized_params.get("add_hs", True)),
                    random_seed=int(normalized_params.get("random_seed", 0xF00D)),
                    prune_rms_thresh=float(normalized_params.get("prune_rms_thresh", 0.5)),
                    optimize=bool(normalized_params.get("optimize", True)),
                )
                model_rows, current_relative_path, current_model_index = _write_ligand_conformer_files(
                    result_mol,
                    output_dir=output_dir,
                    source_path=source_path,
                    start_index=next_index,
                    project_root=project_root,
                )
                for model_row in model_rows:
                    model_row["molecule_id"] = ligand_id
                state = {
                    "has_hs": bool(normalized_params.get("add_hs", True)),
                    "has_3d": result_mol.GetNumConformers() > 0,
                    "is_minimized": bool(normalized_params.get("optimize", True)),
                }
                output_path = project_root / str(current_relative_path or "")
            else:
                result_mol = minimize_ligand_molecule(
                    mol,
                    forcefield=str(normalized_params.get("forcefield", "mmff")),
                    max_iters=int(normalized_params.get("max_iters", 200)),
                )
                current_model_index = next_index
                output_path = artifact_path_for_existing(
                    output_dir,
                    role="ligand",
                    source_path=source_path,
                    artifact_name=f"minimized_{current_model_index}",
                    suffix=".sdf",
                )
                _write_ligand_mol(result_mol, output_path)
                current_relative_path = _relative_to_project_root(output_path, project_root=project_root)
                model_rows.append(
                    MoleculeModel.build_row(
                        molecule_id=ligand_id,
                        model_index=int(current_model_index),
                        file_path=current_relative_path,
                        source=ModelSource.RDKIT,
                        energy=None,
                    )
                )
                state = {"has_hs": True, "has_3d": True, "is_minimized": True}

            updates.append(
                {
                    "entity_id": ligand_id,
                    "extra_data": merge_chemistry_metadata(metadata, operation=operation_name, path=output_path, source_path=source_path, params=normalized_params, state={**state, "conformer_count": result_mol.GetNumConformers()}, promote_current=operation_name != "conformers"),
                    "operation_kind": f"chemistry_{operation_name}",
                    "current_path": str(current_relative_path or row.get("current_path") or ""),
                    "current_model_index": None if current_model_index is None else int(current_model_index),
                    "state": {**state, "conformer_count": result_mol.GetNumConformers()},
                    "model_rows": model_rows,
                    "operation_params": {
                        "operation": operation_name,
                        "source_path": str(source_path),
                        "output_path": str(output_path),
                        "current_path": str(current_relative_path or row.get("current_path") or ""),
                        **normalized_params,
                    },
                }
            )
        except Exception as exc:
            failed_rows.append(
                {
                    "entity_id": ligand_id,
                    "source_path": str(row.get("current_path") or row.get("stored_path") or ""),
                    "error": str(exc),
                }
            )
        if progress_cb is not None:
            progress_cb((index / max(1, len(row_list))) * 100.0)
    return {
        "updates": updates,
        "failure_count": len(failed_rows),
        "failure_samples": failed_rows[:10],
        "processed_count": len(row_list),
        "updated_count": len(updates),
    }


def transform_receptor_rows(
    *,
    operation: str,
    output_dir: Path,
    rows: Iterable[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    next_model_index_by_entity: Mapping[int, int] | None = None,
    progress_cb=None,
) -> list[dict[str, Any]]:
    operation_name = str(operation or "").strip().lower()
    if operation_name not in {"fix", "protonate", "minimize"}:
        raise ValueError(f"Unsupported receptor chemistry operation: {operation}")

    normalized_params = dict(params or {})
    project_root = output_dir.expanduser().resolve().parent.parent
    set_default_project_root(project_root)
    next_index_map = {int(key): int(value) for key, value in dict(next_model_index_by_entity or {}).items()}
    row_list = [dict(row) for row in rows]
    updates: list[dict[str, Any]] = []
    for index, row in enumerate(row_list, start=1):
        receptor_id = int(row.get("id") or 0)
        if receptor_id <= 0:
            continue
        metadata = decode_metadata(row.get("extra_data"))
        source_path = receptor_working_path(
            row,
            structure_source=str(normalized_params.get("structure_source") or "current"),
        )
        current_model_index = 0 if not bool(row.get("has_3d")) and row.get("current_model_index") is None else int(next_index_map.get(receptor_id, 0))
        target_path: Path
        if operation_name == "fix":
            target_path = artifact_path_for_existing(
                output_dir,
                role="receptor",
                source_path=source_path,
                artifact_name=f"fixed_{current_model_index}",
                suffix=".pdb",
            )
            fix_receptor_pdb_file(
                source_path=source_path,
                output_path=target_path,
                add_missing_residues=bool(normalized_params.get("add_missing_residues", True)),
                add_missing_atoms=bool(normalized_params.get("add_missing_atoms", True)),
                replace_nonstandard=bool(normalized_params.get("replace_nonstandard", True)),
                remove_heterogens=bool(normalized_params.get("remove_heterogens", False)),
                keep_water=bool(normalized_params.get("keep_water", True)),
            )
            state = {"has_hs": False, "has_3d": True, "is_minimized": False, "fixed_with": "pdbfixer"}
        elif operation_name == "protonate":
            method = str(normalized_params.get("method") or "reduce").strip().lower()
            suffix = ".pdb"  # pdb2pqr too: it writes the PQR as a sidecar, we register the PDB
            target_path = artifact_path_for_existing(
                output_dir,
                role="receptor",
                source_path=source_path,
                artifact_name=f"protonated_{current_model_index}",
                suffix=suffix,
            )
            if method == "reduce":
                protonate_receptor_reduce_file(source_path=source_path, output_path=target_path)
            elif method == "pdb2pqr":
                protonate_receptor_pdb2pqr_file(
                    source_path=source_path,
                    output_path=target_path,
                    forcefield=str(normalized_params.get("forcefield", "AMBER")),
                    ph=float(normalized_params.get("ph", 7.0)),
                )
            else:
                raise ValueError("protonate_receptors method must be 'reduce' or 'pdb2pqr'.")
            state = {"has_hs": True, "has_3d": True, "is_minimized": False, "protonation_model": method}
        else:
            target_path = artifact_path_for_existing(
                output_dir,
                role="receptor",
                source_path=source_path,
                artifact_name=f"minimized_{current_model_index}",
                suffix=".pdb",
            )
            forcefields = tuple(normalized_params.get("forcefields") or ("amber14-all.xml",))
            minimize_receptor_openmm_file(
                source_path=source_path,
                output_path=target_path,
                forcefields=forcefields,
                max_iterations=int(normalized_params.get("max_iterations", 500)),
                tolerance_kj_mol=float(normalized_params.get("tolerance_kj_mol", 10.0)),
            )
            state = {"has_hs": True, "has_3d": True, "is_minimized": True, "forcefield": list(forcefields)}

        current_relative_path = _relative_to_project_root(target_path, project_root=project_root)
        updates.append(
            {
                "entity_id": receptor_id,
                "extra_data": merge_chemistry_metadata(metadata, operation=f"receptor_{operation_name}", path=target_path, source_path=source_path, params=normalized_params, state=state),
                "operation_kind": f"chemistry_receptor_{operation_name}",
                "current_path": current_relative_path,
                "current_model_index": current_model_index,
                "state": state,
                "model_rows": [
                    MoleculeModel.build_row(
                        molecule_id=receptor_id,
                        model_index=current_model_index,
                        file_path=current_relative_path,
                        source=ModelSource.IMPORTED,
                        energy=None,
                    )
                ],
                "operation_params": {
                    "operation": operation_name,
                    "source_path": str(source_path),
                    "output_path": str(target_path),
                    **normalized_params,
                },
            }
        )
        if progress_cb is not None:
            progress_cb((index / max(1, len(row_list))) * 100.0)
    return updates


__all__ = [
    "chemistry_current_entry",
    "decode_metadata",
    "ligand_working_path",
    "merge_chemistry_metadata",
    "receptor_working_path",
    "transform_ligand_rows",
    "transform_receptor_rows",
]
