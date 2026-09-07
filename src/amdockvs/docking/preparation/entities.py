"""Preparing database rows: one molecule row in, one PDBQT file out.

The `ad4` profile's row-based half (`preparation.shards` is the screening twin). It writes
one file per entity under the job output directory and reports the metadata updates the
caller persists — it never touches the database itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from amdockvs.core.paths import preferred_molecule_path
from amdockvs.docking.preparation.meeko import (
    prepare_ligand_vina_pdbqt,
    prepare_ligand_vina_pdbqt_from_mol,
    prepare_receptor_vina_pdbqt,
)


def _storage_key(entity_kind: str, entity_id: int, *, engine: str) -> str:
    return f"{entity_kind}_{int(entity_id):09d}_{str(engine).strip().lower()}"


def _load_ligand_mol_for_preparation(source_path: Path):
    from amdockvs.io.formats import is_readable, read_mol

    if not is_readable(source_path):
        raise ValueError(f"Ligand preparation does not support format '{source_path.suffix}'.")
    mol = read_mol(source_path)
    if mol is None:
        raise RuntimeError(f"Could not parse ligand file: {source_path}")
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


__all__ = ["prepare_entities_rows", "receptor_excluded_resnames"]
