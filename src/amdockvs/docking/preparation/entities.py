"""Preparing database rows: one molecule's bytes in, one PDBQT payload out.

The `ad4` profile's row-based half (`preparation.shards` is the screening twin). Each row
carries its structure (`structure`, `structure_kind`); the programs read paths, so the bytes
go through a scratch file and the PDBQT comes back as a payload the caller's sink stores —
it never touches the database itself.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ms_flow.payloads import scratch_file

from amdockvs.core.constants import SERIES_PREPARED
from amdockvs.core.payloads import structure_payload
from amdockvs.docking.preparation.adtools import (
    prepare_ligand_vina_pdbqt,
    prepare_ligand_vina_pdbqt_from_mol,
    prepare_receptor_vina_pdbqt,
)

_LOG = logging.getLogger(__name__)


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


# The coordination metals that AD4 types (and so autogrid4 maps); any other (CD, CO, NI...) is left
# out of the PDBQT. ponytail: Vina alone types Co/Ni/Cd/Hg/Cu too, widen per engine if asked.
TYPED_METALS = frozenset({"ZN", "FE", "MN", "MG", "CA", "CU"})


def receptor_excluded_resnames(*, keep_waters: bool, keep_cofactors: bool) -> frozenset[str]:
    """HET codes to leave out of the receptor PDBQT, from the same curated lists import uses."""
    from amdockvs.io.receptor_preview import hetero_codes

    excluded: set[str] = set(hetero_codes("coordination_metal") - TYPED_METALS)
    if not keep_waters:
        excluded |= hetero_codes("water")
    if not keep_cofactors:
        excluded |= hetero_codes("cofactor")
    return frozenset(excluded)


def cofactor_codes(data: bytes | str, kind: str) -> tuple[str, ...]:
    """The cofactor HET codes in one receptor structure (PDB or mmCIF text), sorted: the
    residues ``keep_cofactors`` keeps or drops. One reading for preparation and for the
    Receptors step's warning, so the two never disagree about what a receptor holds.

    Read straight from the text, no conversion: the warning runs it over every receptor in
    scope, a whole proteome included. A structure that names no cofactor anywhere costs one
    byte search per code."""
    from amdockvs.io.receptor_preview import hetero_codes

    raw = data.encode() if isinstance(data, str) else bytes(data)
    cofactors = hetero_codes("cofactor")
    if b"HETATM" not in raw or not any(code.encode() in raw for code in cofactors):
        return ()
    names: set[str] = set()
    if str(kind).lower() in {"cif", "mmcif"}:
        fields: list[bytes] = []
        column = None
        for line in raw.splitlines():
            if line.startswith(b"_atom_site."):
                fields.append(line.strip())
                column = None
            elif line.startswith(b"HETATM"):
                if column is None:
                    wanted = (b"_atom_site.auth_comp_id", b"_atom_site.label_comp_id")
                    column = next((fields.index(f) for f in wanted if f in fields), -1)
                if column >= 0:
                    tokens = line.split()
                    if column < len(tokens):
                        names.add(tokens[column].decode(errors="replace").strip("'\"").upper())
    else:  # PDB (and PDBQT): the residue name sits in columns 18-20
        names = {line[17:20].decode(errors="replace").strip().upper()
                 for line in raw.splitlines() if line.startswith(b"HETATM")}
    return tuple(sorted(names & cofactors))


def _prepared(text: str):
    return structure_payload(text, "pdbqt", SERIES_PREPARED)


def _prepare_row(row: Mapping[str, Any], *, entity_kind: str, excluded_resnames: frozenset[str],
                 ccd_components: bool = False, ion_charges: bool = False):
    """(prepared, flex, warnings) of one row: a payload each, or the row's own payload id when
    it already is a PDBQT (no second copy).

    The warnings come back rather than being dropped here: they are the only account of a
    residue ms_adtools could not type or charged to a fraction, and a PDBQT carries no trace of it."""
    data, kind = row.get("structure"), str(row.get("structure_kind") or "")
    if data is None:
        raise ValueError(f"{entity_kind} {row.get('id')} has no structure.")
    if kind == "pdbqt":
        return int(row["structure_payload_id"]), None, []
    with scratch_file(data, f".{kind}") as source_path, tempfile.TemporaryDirectory() as out:
        output_path = Path(out) / f"{entity_kind}.pdbqt"
        if entity_kind == "ligand":
            return _prepared(_prepare_ligand(source_path=source_path, output_path=output_path).read_text()), None, []
        result = prepare_receptor_vina_pdbqt(
            source_path=source_path,
            output_path=output_path,
            flexible_residues=[str(k) for k in (row.get("flexible_residues") or [])],
            exclude_resnames=excluded_resnames,
            # The CCD pass types every non-standard residue, not just cofactors (modified residues,
            # other kept HET groups). Run for a receptor with no cofactor, "Include cofactors"
            # would retype it too: in a mixed batch, the receptors it has nothing to keep for.
            ccd_components=ccd_components and bool(cofactor_codes(data, kind)),
            ion_charges=ion_charges,
        )
        if result.artifact is None or result.artifact.path is None:
            raise RuntimeError(f"{entity_kind} preparation did not return an output artifact.")
        flex = result.metadata.get("flex_path")
        return (_prepared(Path(result.artifact.path).read_text()),
                _prepared(Path(flex).read_text()) if flex else None,
                list(result.warnings))


def prepare_entities_rows(
    *,
    entity_kind: str,
    engine: str,
    rows: list[Mapping[str, Any]],
    keep_waters: bool = False,
    keep_cofactors: bool = False,
    ion_charges: bool = False,
    progress_cb=None,
) -> dict[str, Any]:
    if entity_kind not in {"ligand", "receptor"}:
        raise ValueError("prepare_entities_rows only supports entity_kind='ligand' or 'receptor'.")
    if str(engine).strip().lower() != "ad4":
        raise ValueError(f"Unsupported docking preparation engine: {engine}")
    excluded_resnames = (
        receptor_excluded_resnames(keep_waters=keep_waters, keep_cofactors=keep_cofactors)
        if entity_kind == "receptor"
        else frozenset()
    )
    updates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        entity_id = int(row.get("id") or 0)
        try:
            if entity_id <= 0:
                raise ValueError(f"Invalid {entity_kind} row: missing entity id.")
            prepared, flex, row_warnings = _prepare_row(
                row, entity_kind=entity_kind, excluded_resnames=excluded_resnames,
                # Only the kept cofactors need the CCD pass, so a dry receptor is typed and
                # charged exactly as before - and so is one that has no cofactor (_prepare_row).
                ccd_components=bool(keep_cofactors) and entity_kind == "receptor",
                ion_charges=bool(ion_charges) and entity_kind == "receptor",
            )
            updates.append({"entity_id": entity_id, "engine": str(engine), "prepared": prepared, "flex": flex,
                            "warnings": [str(message) for message in row_warnings]})
            for message in row_warnings:
                _LOG.warning("%s %s: %s", entity_kind, entity_id, message)
                warnings.append({"entity_id": entity_id, "message": str(message)})
        except Exception as exc:
            failures.append({"entity_id": entity_id, "error": str(exc)})
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
        # Not a failure and not nothing: a receptor that prepared, with something about it the
        # file cannot say. The log has them whatever the caller does with these.
        "warnings": warnings,
    }


__all__ = ["prepare_entities_rows", "receptor_excluded_resnames"]
