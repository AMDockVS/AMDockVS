from __future__ import annotations


def _standardize_api():
    from rdkit.Chem.MolStandardize import rdMolStandardize

    return rdMolStandardize


def add_explicit_hydrogens(mol):
    from rdkit import Chem

    # addCoords so Hs land in sensible positions when the heavy atoms already have 3D coords.
    return Chem.AddHs(mol, addCoords=mol.GetNumConformers() > 0)


def prepare_import_structure(mol, *, add_hs: bool, gen_3d: bool, canonical_tautomer: bool):
    """Apply the opt-in import prep steps to a ligand fragment, each only when it's missing.

    Returns (mol, changed). Cheap ops (Hs, tautomer) first; 3D embedding — the expensive one —
    last and only if there's no conformer yet.
    """
    from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs

    work = mol
    changed = False
    if canonical_tautomer:
        work = _standardize_api().TautomerEnumerator().Canonicalize(work)
        changed = True
    if gen_3d and not mol_has_3d(work):
        from amdockvs.chemistry.conformers import generate_3d_molecule

        # generate_3d_molecule adds Hs itself, so this also satisfies add_hs.
        return generate_3d_molecule(work, add_hs=True), True
    if add_hs and not mol_has_explicit_hs(work):
        work = add_explicit_hydrogens(work)
        changed = True
    return work, changed


def remove_explicit_hydrogens(mol):
    from rdkit import Chem

    return Chem.RemoveHs(mol)


def cleanup_molecule(
    mol,
    *,
    fragment_parent: bool = True,
    neutralize: bool = True,
    canonicalize_tautomer: bool = False,
):
    from rdkit import Chem

    standardize = _standardize_api()
    work_mol = Chem.Mol(mol)
    work_mol = standardize.Cleanup(work_mol)
    if fragment_parent:
        work_mol = standardize.FragmentParent(work_mol)
    if neutralize:
        work_mol = standardize.Uncharger().uncharge(work_mol)
    if canonicalize_tautomer:
        work_mol = standardize.TautomerEnumerator().Canonicalize(work_mol)
    Chem.SanitizeMol(work_mol)
    return work_mol


def standardize_smiles(
    smiles: str,
    *,
    fragment_parent: bool = True,
    neutralize: bool = True,
    canonicalize_tautomer: bool = False,
) -> str:
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("RDKit could not parse SMILES.")
    cleaned = cleanup_molecule(
        mol,
        fragment_parent=fragment_parent,
        neutralize=neutralize,
        canonicalize_tautomer=canonicalize_tautomer,
    )
    return str(Chem.MolToSmiles(cleaned, canonical=True))


__all__ = [
    "add_explicit_hydrogens",
    "cleanup_molecule",
    "prepare_import_structure",
    "remove_explicit_hydrogens",
    "standardize_smiles",
]
