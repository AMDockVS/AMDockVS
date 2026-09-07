from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from amdockvs.chemistry.pipeline import LIGAND_STEPS, normalize_steps, run_pipeline
from amdockvs.chemistry.tools import (
    fix_receptor_pdb_file,
    minimize_receptor_openmm_file,
    protonate_receptor_pdb2pqr_file,
    protonate_receptor_reduce_file,
)
from amdockvs.io.formats import as_pdb
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
    from amdockvs.io.formats import is_readable, read_mol

    if not is_readable(path):
        raise ValueError(f"Chemistry ligand tools do not support format '{path.suffix}'.")
    mol = read_mol(path)
    if mol is None:
        raise ValueError(f"Could not parse ligand file: {path}")
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


def _ligand_state(steps: Sequence[tuple[str, Mapping[str, Any]]], result_mol) -> dict[str, Any]:
    """The flags after the last step that speaks about each one.

    Same values the single-operation branches used to set by hand; folding them makes a
    multi-step run report what it actually produced instead of what its last step alone did.
    """
    has_hs = False
    is_minimized = False
    for name, step_params in steps:
        if name == "standardize":
            has_hs, is_minimized = False, False
        elif name == "protonate":
            has_hs, is_minimized = True, False
        elif name == "generate_3d":
            has_hs = bool(step_params.get("add_hs", True))
            is_minimized = bool(
                result_mol.HasProp("_amdock_is_minimized")
                and result_mol.GetBoolProp("_amdock_is_minimized")
            )
        elif name == "conformers":
            has_hs = bool(step_params.get("add_hs", True))
            is_minimized = bool(step_params.get("optimize", True))
        elif name == "minimize":
            has_hs, is_minimized = True, True
    return {
        "has_hs": has_hs,
        "has_3d": result_mol.GetNumConformers() > 0,
        "is_minimized": is_minimized,
    }


def transform_ligand_rows(
    *,
    operations: str | Sequence[Any],
    output_dir: Path,
    rows: Iterable[Mapping[str, Any]],
    params: Mapping[str, Any] | None = None,
    next_model_index_by_entity: Mapping[int, int] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """Apply one or more chemistry steps to a batch of ligand rows.

    `operations` is a step list — `[("standardize", {}), ("protonate", {"ph": 7.4})]` — or a
    bare name for a single step. The batch goes through `run_pipeline` once and only the final
    molecule is written, so standardize+protonate+3D leaves one file per ligand, not three.
    """
    resolved_steps = normalize_steps(operations)
    if not resolved_steps:
        raise ValueError("No ligand chemistry operation was given.")
    for name, _step_params in resolved_steps:
        if name not in LIGAND_STEPS:
            raise ValueError(f"Unsupported ligand chemistry operation: {name}")

    normalized_params = dict(params or {})
    project_root = output_dir.expanduser().resolve().parent.parent
    set_default_project_root(project_root)
    next_index_map = {int(key): int(value) for key, value in dict(next_model_index_by_entity or {}).items()}
    row_list = [dict(row) for row in rows]
    updates: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    structure_source = str(normalized_params.get("structure_source") or "current")
    run_id = str(normalized_params.get("run_id") or "run")[:16]
    # ponytail: one shared bag behind each step's own params, and every step takes only the
    # keys its core declares. Two steps that name a parameter alike (`fragment_mode`) share the
    # bag's value — pass it per step to tell them apart.
    resolved_steps = [
        (name, {**normalized_params, **dict(step_params)}) for name, step_params in resolved_steps
    ]
    operation_label = "+".join(name for name, _ in resolved_steps)
    last_name, last_params = resolved_steps[-1]

    # Load the whole batch first: protonation runs once over the set, so the pipeline needs
    # every molecule in hand before the first step. A slot that fails to load carries its
    # exception through the pipeline and lands in `failed_rows` below, still in position.
    sources: list[Path | None] = []
    batch: list[Any] = []
    for index, row in enumerate(row_list, start=1):
        source_path = None
        loaded = None  # a `None` slot is not a failure: there is nothing to write back to
        if int(row.get("id") or 0) > 0:
            try:
                source_path = ligand_working_path(row, structure_source=structure_source)
                loaded = _load_ligand_mol(source_path)
            except Exception as exc:
                loaded = exc
        # One append each, always: the write-back loop pairs these lists with `row_list`.
        sources.append(source_path)
        batch.append(loaded)
        if progress_cb is not None:
            progress_cb((index / max(1, len(row_list))) * 50.0)

    results = run_pipeline(batch, resolved_steps)

    for index, (row, source_path, result) in enumerate(zip(row_list, sources, results), start=1):
        ligand_id = int(row.get("id") or 0)
        if progress_cb is not None:
            progress_cb(50.0 + (index / max(1, len(row_list))) * 50.0)
        if ligand_id <= 0:
            continue
        if result is None or isinstance(result, Exception):
            failed_rows.append(
                {
                    "entity_id": ligand_id,
                    "source_path": str(row.get("current_path") or row.get("stored_path") or ""),
                    "error": str(result) if result is not None else "No structure was produced.",
                }
            )
            continue
        try:
            metadata = decode_metadata(row.get("extra_data"))
            model_rows: list[dict[str, Any]] = []
            current_model_index = row.get("current_model_index")
            next_index = int(next_index_map.get(ligand_id, 0))

            if last_name == "conformers":
                model_rows, current_relative_path, current_model_index = _write_ligand_conformer_files(
                    result,
                    output_dir=output_dir,
                    source_path=source_path,
                    start_index=next_index,
                    project_root=project_root,
                )
                for model_row in model_rows:
                    model_row["molecule_id"] = ligand_id
                output_path = project_root / str(current_relative_path or "")
            else:
                if last_name == "generate_3d":
                    current_model_index = next_index
                    artifact_name = f"model_{current_model_index}_{run_id}"
                elif last_name == "minimize":
                    current_model_index = next_index
                    artifact_name = f"minimized_{current_model_index}"
                else:
                    # standardize / protonate keep whatever model was current, unless the
                    # result has no coordinates at all.
                    current_model_index = current_model_index if result.GetNumConformers() > 0 else None
                    artifact_name = (
                        f"standardized_{run_id}"
                        if last_name == "standardize"
                        else f"protonated_{last_params.get('method') or 'dimorphite'}_{run_id}"
                    )
                output_path = artifact_path_for_existing(
                    output_dir,
                    role="ligand",
                    source_path=source_path,
                    artifact_name=artifact_name,
                    suffix=".sdf",
                )
                _write_ligand_mol(result, output_path)
                current_relative_path = _relative_to_project_root(output_path, project_root=project_root)
                if last_name in {"generate_3d", "minimize"}:
                    model_rows.append(
                        MoleculeModel.build_row(
                            molecule_id=ligand_id,
                            model_index=int(current_model_index),
                            file_path=current_relative_path,
                            source=ModelSource.RDKIT,
                            energy=None,
                        )
                    )

            state = {
                **_ligand_state(resolved_steps, result),
                "conformer_count": result.GetNumConformers(),
            }
            updates.append(
                {
                    "entity_id": ligand_id,
                    "extra_data": merge_chemistry_metadata(metadata, operation=operation_label, path=output_path, source_path=source_path, params=normalized_params, state=state, promote_current=last_name != "conformers"),
                    "operation_kind": f"chemistry_{operation_label}",
                    "current_path": str(current_relative_path or row.get("current_path") or ""),
                    "current_model_index": None if current_model_index is None else int(current_model_index),
                    "state": state,
                    "model_rows": model_rows,
                    "operation_params": {
                        "operation": operation_label,
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
        # pdbfixer, reduce, pdb2pqr and OpenMM all read PDB; the stored structure is mmCIF.
        with as_pdb(source_path) as readable:
            if operation_name == "fix":
                target_path = artifact_path_for_existing(
                    output_dir,
                    role="receptor",
                    source_path=source_path,
                    artifact_name=f"fixed_{current_model_index}",
                    suffix=".pdb",
                )
                fix_receptor_pdb_file(
                    source_path=readable,
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
                    protonate_receptor_reduce_file(source_path=readable, output_path=target_path)
                elif method == "pdb2pqr":
                    protonate_receptor_pdb2pqr_file(
                        source_path=readable,
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
                    source_path=readable,
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
