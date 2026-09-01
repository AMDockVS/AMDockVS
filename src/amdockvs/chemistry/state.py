from __future__ import annotations

from typing import Any


def mol_has_3d(mol: Any) -> bool:
    """Return whether a molecule has at least one 3D conformer."""
    if mol is None or mol.GetNumConformers() == 0:
        return False
    return any(bool(mol.GetConformer(index).Is3D()) for index in range(mol.GetNumConformers()))


def mol_has_explicit_hs(mol: Any) -> bool:
    """Return whether a molecule contains explicit hydrogen atoms."""
    if mol is None:
        return False
    return any(atom.GetAtomicNum() == 1 for atom in mol.GetAtoms())


def molecule_state_metadata(mol: Any) -> dict[str, Any]:
    """Build the normalized state payload derived from a parsed molecule."""
    return {
        "state": {
            "has_3d": mol_has_3d(mol),
            "has_hs": mol_has_explicit_hs(mol),
            "conformer_count": 0 if mol is None else int(mol.GetNumConformers()),
        }
    }


__all__ = ["mol_has_3d", "mol_has_explicit_hs", "molecule_state_metadata"]
