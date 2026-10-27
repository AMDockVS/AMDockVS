"""Turn what the 2D editor drew back into a structure that can replace a molecule's. No files, no DB.

Ketcher keeps the atoms the user did not touch (their order, explicit Hs and z; x/y come back
scaled and shifted) and draws new atoms flat. So a molecule that had 3D gets 3D back: the atoms
the edit kept are pinned at their original coordinates, only the rest is embedded, and the result
is aligned onto the original — the edited ligand stays where it was in the pocket. A 2D molecule
stays 2D, as drawn.
"""
from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import rdFMCS, rdMolAlign
from rdkit.Chem.rdDistGeom import EmbedMolecule

from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs


def changed_molecule(molblock: str, original: Chem.Mol | None) -> Chem.Mol | None:
    """Validate the drawing; None if its canonical isomeric SMILES is unchanged, ignoring ordinary Hs."""
    mol = Chem.MolFromMolBlock(str(molblock or ""), removeHs=False)
    if mol is None or mol.GetNumAtoms() == 0:
        raise ValueError("The editor returned no molecule RDKit can read.")
    if original is not None and _smiles(mol) == _smiles(original):
        return None
    return mol


def edited_structure(molblock: str, original: Chem.Mol | None) -> Chem.Mol | None:
    """The edited molecule, or None when the drawing is still the same compound as `original`.

    Raises ValueError on a drawing RDKit cannot read, or one whose 3D cannot be built."""
    mol = changed_molecule(molblock, original)
    if mol is None:
        return None
    if original is None or not mol_has_3d(original):
        # Ketcher leaves the Hs of atoms the user drew implicit (an "OH" label): give them atoms
        # when the molecule carries explicit Hs, or the viewer shows a bare O next to drawn Hs.
        return Chem.AddHs(mol, addCoords=True) if mol_has_explicit_hs(original if original is not None else mol) else mol
    return _with_original_3d(mol, original)


def _smiles(mol: Chem.Mol) -> str:
    return Chem.MolToSmiles(Chem.RemoveHs(mol))


def _with_original_3d(mol: Chem.Mol, original: Chem.Mol) -> Chem.Mol:
    reference, edited = Chem.RemoveHs(original), Chem.RemoveHs(mol)
    common = rdFMCS.FindMCS([reference, edited], ringMatchesRingOnly=True, completeRingsOnly=True, timeout=10)
    pattern = Chem.MolFromSmarts(common.smartsString) if common.numAtoms else None
    kept = list(zip(reference.GetSubstructMatch(pattern), edited.GetSubstructMatch(pattern))) if pattern else []
    target = Chem.AddHs(edited)  # heavy atoms keep their indices; Hs go after them
    positions = reference.GetConformer()
    # ponytail: pinned atoms keep their distances, the rest is distance geometry with no force-field
    # relax; minimize afterwards (Build) if the new part needs a better geometry.
    coord_map = {new: positions.GetAtomPosition(old) for old, new in kept} if len(kept) >= 3 else {}
    if EmbedMolecule(target, coordMap=coord_map, randomSeed=0xF00D, useRandomCoords=True) < 0:
        raise ValueError("Could not build 3D coordinates for the edited molecule.")
    if coord_map:
        rdMolAlign.AlignMol(target, reference, atomMap=[(new, old) for old, new in kept])
    return target if mol_has_explicit_hs(original) else Chem.RemoveHs(target)


__all__ = ["changed_molecule", "edited_structure"]
