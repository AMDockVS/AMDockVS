from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from rdkit import Chem
from amdockvs.constants import DEFAULT_VINA_BACKEND, DEFAULT_VINA_COMMAND
from amdockvs.docking.gnina import chunk_gpu_tokens
from amdockvs.docking.tools import prepare_ligand_vina_pdbqt, prepare_ligand_vina_pdbqt_from_mol, prepare_receptor_vina_pdbqt
from amdockvs.molecule_paths import normalize_path, preferred_molecule_path


def _decode_metadata(raw_metadata: str | None) -> dict[str, Any]:
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_metadata(metadata: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata or {}), ensure_ascii=True)


def _prepared_entry(metadata: Mapping[str, Any] | None, *, engine: str) -> dict[str, Any] | None:
    prepared = dict((metadata or {}).get("prepared") or {})
    entry = prepared.get(str(engine).strip().lower())
    return dict(entry) if isinstance(entry, dict) else None


def prepared_path_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> Path | None:
    direct_path = str(row.get(f"prepared_{str(engine).strip().lower()}_path") or "").strip()
    if direct_path:
        return normalize_path(direct_path)
    files = dict(row.get("prepared_files") or {})
    if files:
        direct = str(files.get("prepared") or "").strip()
        if direct:
            return normalize_path(direct)
    metadata = _decode_metadata(row.get("metadata_json"))
    entry = _prepared_entry(metadata, engine=engine)
    if entry is None:
        return None
    return normalize_path(entry.get("path"))


def docking_input_path_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> Path:
    prepared_path = prepared_path_from_row(row, engine=engine)
    if prepared_path is not None:
        return prepared_path
    stored_path = preferred_molecule_path(row)
    if stored_path is None:
        raise ValueError(f"Missing stored_path for docking input row: {dict(row)}")
    return stored_path


def grid_from_metadata_json(raw_metadata: str | None, *, engine: str = "ad4") -> dict[str, Any] | None:
    metadata = _decode_metadata(raw_metadata)
    grids = dict(metadata.get("docking_grids") or {})
    entry = grids.get(str(engine).strip().lower())
    return dict(entry) if isinstance(entry, dict) else None


def grid_from_row(row: Mapping[str, Any], *, engine: str = "ad4") -> dict[str, Any] | None:
    payload = row.get("grid_engine_payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    payload = row.get(f"grid_{str(engine).strip().lower()}_payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    return grid_from_metadata_json(row.get("metadata_json"), engine=engine)


def merge_prepared_metadata(
    raw_metadata: str | None,
    *,
    engine: str,
    path: Path,
    source_path: Path,
    entity_kind: str,
    extra: Mapping[str, Any] | None = None,
) -> str:
    metadata = _decode_metadata(raw_metadata)
    prepared = dict(metadata.get("prepared") or {})
    entry = {
        "engine": str(engine),
        "entity_kind": str(entity_kind),
        "path": str(path),
        "source_path": str(source_path),
        "created_at": datetime.now().isoformat(),
    }
    if extra:
        entry.update(dict(extra))
    prepared[str(engine).strip().lower()] = entry
    metadata["prepared"] = prepared
    return _encode_metadata(metadata)


def merge_grid_metadata(
    raw_metadata: str | None,
    *,
    engine: str,
    center: Iterable[float],
    size: Iterable[float],
    spacing: float,
    extra: Mapping[str, Any] | None = None,
) -> str:
    metadata = _decode_metadata(raw_metadata)
    grids = dict(metadata.get("docking_grids") or {})
    entry = {
        "engine": str(engine),
        "center": [float(value) for value in center],
        "size": [float(value) for value in size],
        "spacing": float(spacing),
        "created_at": datetime.now().isoformat(),
    }
    if extra:
        entry.update(dict(extra))
    grids[str(engine).strip().lower()] = entry
    metadata["docking_grids"] = grids
    return _encode_metadata(metadata)


def _storage_key(entity_kind: str, entity_id: int, *, engine: str) -> str:
    return f"{entity_kind}_{int(entity_id):09d}_{str(engine).strip().lower()}"


def _load_ligand_mol_for_preparation(source_path: Path):
    suffix = source_path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        supplier = Chem.SDMolSupplier(str(source_path), removeHs=False)
        mol = supplier[0] if supplier and len(supplier) > 0 else None
    elif suffix == ".mol2":
        mol = Chem.MolFromMol2File(str(source_path), sanitize=True, removeHs=False)
    elif suffix == ".pdb":
        mol = Chem.MolFromPDBFile(str(source_path), sanitize=True, removeHs=False)
    else:
        raise ValueError(f"Ligand preparation does not support format '{suffix}'.")
    if mol is None:
        raise RuntimeError(f"RDKit could not parse ligand file: {source_path}")
    return mol


def _prepare_ligand(
    *,
    source_path: Path,
    output_path: Path,
) -> Path:
    if source_path.suffix.lower() == ".pdbqt":
        result = prepare_ligand_vina_pdbqt(source_path=source_path, output_path=output_path)
        if result.artifact is None or result.artifact.path is None:
            raise RuntimeError("Ligand preparation did not return a PDBQT artifact.")
        return result.artifact.path

    mol = _load_ligand_mol_for_preparation(source_path)
    if mol.GetNumConformers() == 0 or not any(bool(mol.GetConformer(i).Is3D()) for i in range(mol.GetNumConformers())):
        raise RuntimeError(
            "Ligand does not satisfy the has_3d requirement for Vina preparation. "
            "Run chemistry.generate_3d_ligands first."
        )
    result = prepare_ligand_vina_pdbqt_from_mol(mol)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(str(result.payload or ""), encoding="utf-8")
    return output_path


def receptor_excluded_resnames(*, keep_waters: bool, keep_cofactors: bool) -> frozenset[str]:
    """HET codes to leave out of the receptor PDBQT, from the same curated lists import uses."""
    from amdockvs.io.receptor_preview import hetero_codes

    excluded: set[str] = set()
    if not keep_waters:
        excluded |= hetero_codes("water")
    if not keep_cofactors:
        excluded |= hetero_codes("cofactor")
    return frozenset(excluded)


def prepare_entities_rows(
    *,
    entity_kind: str,
    engine: str,
    output_dir: Path,
    rows: list[Mapping[str, Any]],
    keep_waters: bool = False,
    keep_cofactors: bool = False,
    progress_cb=None,
) -> dict[str, Any]:
    if entity_kind not in {"ligand", "receptor"}:
        raise ValueError("prepare_entities_rows only supports entity_kind='ligand' or 'receptor'.")
    if str(engine).strip().lower() != "ad4":
        raise ValueError(f"Unsupported docking preparation engine: {engine}")
    output_dir.mkdir(parents=True, exist_ok=True)
    excluded_resnames = (
        receptor_excluded_resnames(keep_waters=keep_waters, keep_cofactors=keep_cofactors)
        if entity_kind == "receptor"
        else frozenset()
    )
    updates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for index, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        entity_id = int(row.get("id") or 0)
        source_path = preferred_molecule_path(row)
        logical_paths = dict(row.get("_worker_logical_paths") or {})
        logical_source = str(
            logical_paths.get("current_path") or logical_paths.get("stored_path") or source_path or ""
        )
        if entity_id <= 0 or source_path is None:
            failures.append(
                {
                    "entity_id": entity_id,
                    "source_path": logical_source,
                    "error": f"Invalid {entity_kind} row: missing entity id or source path.",
                }
            )
            if progress_cb is not None:
                progress_cb((index / max(1, len(rows))) * 100.0)
            continue
        output_path = output_dir / f"{_storage_key(entity_kind, entity_id, engine=engine)}.pdbqt"
        try:
            if entity_kind == "ligand":
                prepared_path = _prepare_ligand(
                    source_path=source_path,
                    output_path=output_path,
                )
            else:
                result = prepare_receptor_vina_pdbqt(
                    source_path=source_path,
                    output_path=output_path,
                    flexible_residues=[str(k) for k in (row.get("flexible_residues") or [])],
                    exclude_resnames=excluded_resnames,
                )
                if result.artifact is None or result.artifact.path is None:
                    raise RuntimeError(f"{entity_kind} preparation did not return an output artifact.")
                prepared_path = result.artifact.path
            updates.append(
                {
                    "entity_id": entity_id,
                    "engine": str(engine),
                    "prepared_path": str(prepared_path),
                    "source_path": logical_source,
                    "files": {},
                    "operation_kind": f"prepare_{engine}",
                    "operation_params": {
                        "engine": engine,
                        "source_path": logical_source,
                        "prepared_path": str(prepared_path),
                    },
                }
            )
        except Exception as exc:
            failures.append(
                {
                    "entity_id": entity_id,
                    "source_path": logical_source,
                    "error": str(exc),
                }
            )
        if progress_cb is not None:
            progress_cb((index / max(1, len(rows))) * 100.0)
    return {
        "entity_kind": str(entity_kind),
        "engine": str(engine),
        "total": len(rows),
        "success_count": len(updates),
        "failure_count": len(failures),
        "updates": updates,
        "failures": failures,
    }


def _ligand_descriptors_from_row(ligand_row: Mapping[str, object]) -> dict[str, float]:
    """The descriptors computed at import, shipped with the pair so the worker never re-parses
    the ligand file (and LLE/BEI/SEI stop coming out empty)."""
    fields = {"heavy_atoms": "heavy_atom_count", "molecular_weight": "mw", "clogp": "logp",
              "tpsa": "tpsa", "hbd": "hbd", "hba": "hba"}
    values = {key: ligand_row.get(column) for key, column in fields.items()}
    return {key: float(value) for key, value in values.items() if value is not None}


def build_docking_pair(
    *,
    ligand_row: Mapping[str, object],
    receptor_row: Mapping[str, object],
    exhaustiveness: int = 8,
    num_modes: int = 9,
    engine: str = "vina",
    complex_id: int | None = None,
    run_kind: str = "screening",
    box_center: list[float] | tuple[float, float, float] | None = None,
    box_size: list[float] | tuple[float, float, float] | None = None,
    spacing: float | None = None,
    reference_ligand_path: str | Path | None = None,
    reference_receptor_path: str | Path | None = None,
) -> dict[str, object]:
    ligand_source_path = str(ligand_row.get("current_path") or ligand_row.get("stored_path") or "")
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "ligand_artifact_id": int(ligand_row.get("artifact_id") or 0) or None,
        "receptor_artifact_id": int(receptor_row.get("artifact_id") or 0) or None,
        "ligand_path": str(docking_input_path_from_row(ligand_row, engine=engine)),
        "ligand_source_path": ligand_source_path,
        "ligand_descriptors": _ligand_descriptors_from_row(ligand_row),
        "receptor_path": str(docking_input_path_from_row(receptor_row, engine=engine)),
        "reference_ligand_path": str(reference_ligand_path or ""),
        "reference_receptor_path": str(reference_receptor_path or ""),
        "exhaustiveness": int(exhaustiveness),
        "num_modes": int(num_modes),
        "box_center": None if box_center is None else [float(value) for value in box_center],
        "box_size": None if box_size is None else [float(value) for value in box_size],
        "spacing": None if spacing is None else float(spacing),
    }


def build_failed_docking_pair(
    *,
    ligand_row: Mapping[str, object],
    receptor_row: Mapping[str, object],
    reason: str,
    complex_id: int | None = None,
    run_kind: str = "screening",
    reference_ligand_path: str | Path | None = None,
    reference_receptor_path: str | Path | None = None,
) -> dict[str, object]:
    ligand_source_path = str(ligand_row.get("current_path") or ligand_row.get("stored_path") or "")
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "ligand_artifact_id": int(ligand_row.get("artifact_id") or 0) or None,
        "receptor_artifact_id": int(receptor_row.get("artifact_id") or 0) or None,
        "ligand_path": str(ligand_row.get("prepared_engine_path") or ligand_row.get("current_path") or ligand_row.get("stored_path") or ""),
        "ligand_source_path": ligand_source_path,
        "receptor_path": str(receptor_row.get("prepared_engine_path") or receptor_row.get("current_path") or receptor_row.get("stored_path") or ""),
        "reference_ligand_path": str(reference_ligand_path or ""),
        "reference_receptor_path": str(reference_receptor_path or ""),
        "invalid_reason": str(reason or "Invalid docking pair."),
    }


def iter_docking_batches_from_rows(
    *,
    ligands: Iterable[Mapping[str, object]] | Callable[[int], Iterable[Mapping[str, object]]],
    receptors: Iterable[Mapping[str, object]],
    output_dir: str | Path,
    batch_size: int,
    exhaustiveness: int = 8,
    num_modes: int = 9,
    box_center: list[float] | tuple[float, float, float] | None = None,
    box_size: list[float] | tuple[float, float, float] | None = None,
    scoring_function: str = "vina",
    vina_backend: str = DEFAULT_VINA_BACKEND,
    vina_command: str = DEFAULT_VINA_COMMAND,
    vina_cpu: int = 1,
    seed: int = 0,
    spacing: float = 0.375,
    energy_range: float = 3.0,
    min_rmsd: float = 1.0,
    run_id: str = "",
    protocol_metadata: Mapping[str, object] | None = None,
    engine: str = "vina",
    preparation_engine: str | None = None,
) -> Iterator[dict[str, object]]:
    # Prepared inputs/grids are stored under the preparation engine (e.g. AutoDock4
    # reuses Vina pdbqt prep); the chunk's `engine` tag selects the docking runner.
    prep_engine = str(preparation_engine or engine)
    protocol_payload = dict(protocol_metadata or {})
    normalized_batch_size = max(1, int(batch_size))
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    batch_index = 0

    receptor_rows = [dict(row) for row in receptors]
    if not receptor_rows:
        return

    # Receptor-major: each receptor gets its own ligand stream, already filtered by the
    # database (the "already docked" guard is a subquery, not a set held in memory).
    if callable(ligands):
        ligands_for = ligands
    elif not isinstance(ligands, Iterator):
        ligands_for = lambda _receptor_id: ligands  # noqa: E731 — re-iterable by construction
    else:
        raise TypeError(
            "ligands must be a callable(receptor_id) -> iterable, or a re-iterable source: "
            "a plain iterator would be exhausted after the first receptor."
        )

    batch: list[dict[str, object]] = []
    for receptor_row in receptor_rows:
        for ligand in ligands_for(int(receptor_row.get("id") or 0)):
            ligand_row = dict(ligand)
            pair_grid = None
            effective_center = box_center
            effective_size = box_size
            effective_spacing = spacing
            if effective_center is None or effective_size is None:
                pair_grid = grid_from_row(receptor_row, engine=prep_engine)
                if pair_grid is not None:
                    resolved_center = tuple(float(value) for value in (pair_grid.get("center") or ()))
                    resolved_size = tuple(float(value) for value in (pair_grid.get("size") or ()))
                    if len(resolved_center) == 3 and len(resolved_size) == 3:
                        effective_center = resolved_center
                        effective_size = resolved_size
                        effective_spacing = float(pair_grid.get("spacing") or spacing)
            if effective_center is None or effective_size is None:
                batch.append(
                    build_failed_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        reason=(
                            f"Receptor {int(receptor_row.get('id') or 0)} requires explicit "
                            "box_center/box_size or a stored grid."
                        ),
                    )
                )
            else:
                try:
                    batch.append(
                        build_docking_pair(
                            ligand_row=ligand_row,
                            receptor_row=receptor_row,
                            exhaustiveness=exhaustiveness,
                            num_modes=num_modes,
                            engine=prep_engine,
                            box_center=effective_center,
                            box_size=effective_size,
                            spacing=effective_spacing,
                        )
                    )
                except Exception as exc:
                    batch.append(
                        build_failed_docking_pair(
                            ligand_row=ligand_row,
                            receptor_row=receptor_row,
                            reason=str(exc),
                        )
                    )
            if len(batch) >= normalized_batch_size:
                batch_index += 1
                yield {
                    "pairs": list(batch),
                    "engine": str(engine),
                    "output_dir": str(resolved_output_dir),
                    "box_center": [],
                    "box_size": [],
                    "scoring_function": str(scoring_function),
                    "vina_backend": str(vina_backend or "python"),
                    "vina_command": str(vina_command or "vina"),
                    "vina_cpu": int(vina_cpu),
                    "seed": int(seed),
                    "spacing": float(spacing),
                    "energy_range": float(energy_range),
                    "min_rmsd": float(min_rmsd),
                    "run_id": str(run_id or ""),
                    "protocol_metadata": protocol_payload,
                    **chunk_gpu_tokens(engine, scoring_function),
                    "report_name": f"batch_{batch_index:06d}.json",
                }
                batch = []
    if batch:
        batch_index += 1
        yield {
            "pairs": list(batch),
            "engine": str(engine),
            "output_dir": str(resolved_output_dir),
            "box_center": [],
            "box_size": [],
            "scoring_function": str(scoring_function),
            "vina_backend": str(vina_backend or "python"),
            "vina_command": str(vina_command or "vina"),
            "vina_cpu": int(vina_cpu),
            "seed": int(seed),
            "spacing": float(spacing),
            "energy_range": float(energy_range),
            "min_rmsd": float(min_rmsd),
            "run_id": str(run_id or ""),
            "protocol_metadata": protocol_payload,
            **chunk_gpu_tokens(engine, scoring_function),
            "report_name": f"batch_{batch_index:06d}.json",
        }


__all__ = [
    "build_docking_pair",
    "build_failed_docking_pair",
    "docking_input_path_from_row",
    "grid_from_metadata_json",
    "grid_from_row",
    "iter_docking_batches_from_rows",
    "merge_grid_metadata",
    "merge_prepared_metadata",
    "prepare_entities_rows",
    "receptor_excluded_resnames",
    "prepared_path_from_row",
]
