from __future__ import annotations

from pathlib import Path
from typing import Any


# ms_contactmap's vocabulary is ours except for these two names; everything else
# ("hydrophobic", "salt_bridge", "pi_stacking", "pi_cation", "halogen_bond",
# "water_bridge") passes through unchanged.
_INTERACTION_ALIASES = {
    "hbond": "hydrogen_bond",
    "metal_coordination": "metal_complex",
}


def _element_from_pdb_line(line: str) -> str:
    element = line[76:78].strip() if len(line) >= 78 else ""
    if not element:
        parts = line.split()
        element = parts[-1] if parts else ""
    return "".join(ch for ch in element.upper() if ch.isalpha())[:2]


def _interaction_type(name: str) -> str:
    key = str(name or "").strip().lower()
    return _INTERACTION_ALIASES.get(key, key or "interaction")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except Exception:
        pass
    return str(value)


def _rdkit_mol(path: Path, *, pose_rank: int = 1):
    try:
        from rdkit import Chem
    except ImportError:
        return None
    suffix = path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        supplier = Chem.SDMolSupplier(str(path), sanitize=False, removeHs=False)
        index = max(0, int(pose_rank or 1) - 1)
        return supplier[index] if supplier and len(supplier) > index else None
    if suffix == ".mol2":
        return Chem.MolFromMol2File(str(path), sanitize=False, removeHs=False)
    if suffix in {".pdb", ".pdbqt", ".ent"}:
        return Chem.MolFromPDBFile(str(path), sanitize=False, removeHs=False)
    return None


def _write_ligand_pdb(path: Path, *, pose_rank: int, output_path: Path) -> bool:
    mol = _rdkit_mol(path, pose_rank=pose_rank)
    if mol is None:
        return False
    try:
        from rdkit import Chem

        Chem.MolToPDBFile(mol, str(output_path))
        return output_path.exists()
    except Exception:
        return False


_AUTODOCK_ELEMENT = {"A": "C", "NA": "N", "OA": "O", "SA": "S", "HD": "H", "HS": "H", "NS": "N"}


def as_pdb_block(
    text: str, *, chain: str = "", resnum: int | None = None, first_serial: int = 1
) -> str:
    """Heavy-atom coordinate lines, in the canonical PDB form ms_contactmap reads.

    Everything here exists so the detector types atoms the way we mean them and its
    report comes back addressed by our own serials:

    * receptors reach us as PDBQT, whose partial-charge + AutoDock-type tail past column
      66 buries columns 77-78 -- and that element field is what the detector assigns
      atom roles from, so the tail goes and a real element symbol goes back in;
    * hydrogens are dropped and the survivors renumbered from 1, so the serials in the
      complex are contiguous and ours;
    * two concatenated blocks can't both keep their END, and the second must continue the
      first's numbering (``first_serial``) instead of restarting at 1.

    ``chain``/``resnum`` stamp those columns: RDKit writes the ligand with neither, and a
    blank chain makes downstream tools fail to find the residue they were just handed.
    """
    lines: list[str] = []
    serial = int(first_serial)
    for raw in text.splitlines():
        if raw.startswith(("ATOM", "HETATM")):
            tail = raw[66:].split()
            # Only the AutoDock pseudo-types get translated; anything else is already an
            # element symbol and must keep both letters (Cl/Br/Mg -- truncating turned every
            # chlorine into a carbon, and the ligand then failed to match its own SMILES).
            # Letters only: RDKit appends the formal charge to the element field
            # ("N1+" for a protonated amine), and keeping it made the composition
            # check downstream read that atom as an element of its own.
            token = "".join(ch for ch in (tail[-1] if tail else "") if ch.isalpha())
            element = _AUTODOCK_ELEMENT.get(token.upper(), token[:2].capitalize())
            if (element or _element_from_pdb_line(raw)) == "H":
                continue
            line = f"{raw[:66]:<66}{'':<10}{element:>2}" if element else raw[:66]
            line = f"{line[:6]}{serial:>5}{line[11:]}"
            serial += 1
            if chain:
                line = f"{line[:21]}{chain[0]}{line[22:]}"
            if resnum is not None:
                line = f"{line[:22]}{int(resnum):>4}{line[26:]}"
            lines.append(line)
        elif raw.startswith("TER"):
            lines.append(raw.rstrip())
    # ponytail: CONECT dropped rather than remapped -- bonds are re-perceived from geometry
    # anyway once the hydrogens are gone. Remap them if a ligand ever comes out mis-bonded.
    return "\n".join(lines)


def atom_count(block: str) -> int:
    return sum(1 for line in block.splitlines() if line.startswith(("ATOM", "HETATM")))



def collect_interaction_rows(
    *,
    pose_path: str | Path,
    receptor_path: str | Path,
    pose_rank: int = 1,
) -> list[dict]:
    """Protein-ligand interactions for one pose, from ms_contactmap's native detector.

    Single provider by design: ms_contactmap is ours, so a second detector would only
    add a vocabulary to reconcile and a licence to inherit. The diagram viewer builds
    the very same :class:`~ms_contactmap.Diagram`, so the table and the drawing can
    never disagree about what was detected.
    """
    from amdockvs.docking.diagram import build_pose_diagram

    diagram = build_pose_diagram(pose_path=pose_path, receptor_path=receptor_path, pose_rank=pose_rank)
    if diagram is None:
        return []
    residues = {residue.key: residue.ref for residue in diagram.residues}
    rows: list[dict[str, Any]] = []
    for interaction in diagram.interactions:
        ref = residues.get(interaction.residue_key)
        if ref is None:  # an interaction whose residue never made it into the diagram
            continue
        distance = float(getattr(interaction, "distance", 0.0) or 0.0)
        rows.append(
            {
                "interaction_type": _interaction_type(interaction.kind),
                "residue": f"{ref.name}{ref.number}:{ref.chain}",
                "residue_index": int(ref.number or 0),
                "distance": round(distance, 3) if distance else None,
                "geometry": {
                    "method": "ms_contactmap",
                    "provider": "ms_contactmap",
                    "provider_interaction": str(interaction.kind),
                    "provider_payload": _json_safe(
                        {
                            "ligand_is_donor": interaction.ligand_is_donor,
                            "protein_atom": interaction.protein_atom,
                            "angle": interaction.angle,
                            "via_water": interaction.via_water,
                        }
                    ),
                },
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row["interaction_type"]),
            int(row.get("residue_index") or 0),
            str(row["residue"]),
        ),
    )


__all__ = ["collect_interaction_rows"]
