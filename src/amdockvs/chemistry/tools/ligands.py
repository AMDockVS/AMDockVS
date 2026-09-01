from __future__ import annotations


_ORGANIC_ATOMIC_NUMBERS = {
    1,   # H
    5,   # B
    6,   # C
    7,   # N
    8,   # O
    9,   # F
    14,  # Si
    15,  # P
    16,  # S
    17,  # Cl
    34,  # Se
    35,  # Br
    53,  # I
}


def _select_fragment(work_mol, *, fragment_mode: str | None = None, fragment_parent: bool = True):
    from rdkit.Chem.MolStandardize import rdMolStandardize

    normalized_mode = str(fragment_mode or "").strip().lower()
    if not normalized_mode:
        normalized_mode = "largest" if fragment_parent else "keep"
    if normalized_mode == "keep":
        return work_mol
    if normalized_mode not in {"largest", "largest_organic"}:
        raise ValueError(f"Unsupported fragment selection mode: {fragment_mode}")
    chooser = rdMolStandardize.LargestFragmentChooser(preferOrganic=normalized_mode == "largest_organic")
    selected = chooser.choose(work_mol)
    if selected is None or selected.GetNumAtoms() <= 0:
        raise ValueError("Could not resolve a valid ligand fragment.")
    return selected


def _has_metal_atoms(work_mol) -> bool:
    return any(
        atom.GetAtomicNum() > 0 and atom.GetAtomicNum() not in _ORGANIC_ATOMIC_NUMBERS
        for atom in work_mol.GetAtoms()
    )


def _is_simple_ion(work_mol) -> bool:
    atom_count = int(work_mol.GetNumAtoms() or 0)
    formal_charge = int(sum(atom.GetFormalCharge() for atom in work_mol.GetAtoms()))
    heavy_atoms = int(sum(1 for atom in work_mol.GetAtoms() if atom.GetAtomicNum() > 1))
    carbon_atoms = int(sum(1 for atom in work_mol.GetAtoms() if atom.GetAtomicNum() == 6))
    return atom_count <= 1 or (heavy_atoms <= 2 and carbon_atoms == 0 and formal_charge != 0)


def _prepare_ligand_candidate(
    mol,
    *,
    fragment_mode: str | None = None,
    fragment_parent: bool = True,
    filter_metals: bool = False,
    filter_simple_ions: bool = False,
):
    from rdkit import Chem

    work_mol = Chem.Mol(mol)
    work_mol = _select_fragment(
        work_mol,
        fragment_mode=fragment_mode,
        fragment_parent=fragment_parent,
    )
    Chem.SanitizeMol(work_mol)
    if filter_simple_ions and _is_simple_ion(work_mol):
        raise ValueError("Ligand was filtered because it resolves to a simple ion.")
    if filter_metals and _has_metal_atoms(work_mol):
        raise ValueError("Ligand was filtered because it contains metal atoms.")
    return work_mol


def standardize_ligand_molecule(
    mol,
    *,
    fragment_parent: bool = True,
    fragment_mode: str | None = None,
    neutralize: bool = True,
    canonicalize_tautomer: bool = False,
):
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize

    work_mol = Chem.Mol(mol)
    work_mol = rdMolStandardize.Cleanup(work_mol)
    work_mol = _select_fragment(
        work_mol,
        fragment_mode=fragment_mode,
        fragment_parent=fragment_parent,
    )
    if neutralize:
        work_mol = rdMolStandardize.Uncharger().uncharge(work_mol)
    if canonicalize_tautomer:
        work_mol = rdMolStandardize.TautomerEnumerator().Canonicalize(work_mol)
    Chem.SanitizeMol(work_mol)
    return work_mol


def protonate_ligand_molecule(mol):
    from rdkit import Chem

    return Chem.AddHs(Chem.Mol(mol))


def generate_ligand_3d(
    mol,
    *,
    add_hs: bool = True,
    random_seed: int = 0xF00D,
    optimize: bool = True,
    fragment_mode: str = "largest_organic",
    filter_metals: bool = True,
    filter_simple_ions: bool = True,
):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work_mol = _prepare_ligand_candidate(
        mol,
        fragment_mode=fragment_mode,
        fragment_parent=True,
        filter_metals=filter_metals,
        filter_simple_ions=filter_simple_ions,
    )
    if add_hs:
        work_mol = Chem.AddHs(work_mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = int(random_seed)
    conf_id = AllChem.EmbedMolecule(work_mol, params)
    print(f"RDKit generated a 3D conformer: {conf_id}")
    if conf_id < 0:
        raise ValueError("RDKit could not generate a 3D conformer.")
    optimized = False
    if optimize:
        if AllChem.MMFFHasAllMoleculeParams(work_mol):
            try:
                AllChem.MMFFOptimizeMolecule(work_mol, confId=conf_id)
                optimized = True
            except Exception:
                optimized = False
        if not optimized and AllChem.UFFHasAllMoleculeParams(work_mol):
            try:
                AllChem.UFFOptimizeMolecule(work_mol, confId=conf_id)
                optimized = True
            except Exception:
                optimized = False
    work_mol.SetBoolProp("_amdock_is_minimized", bool(optimized))
    return work_mol


def minimize_ligand_molecule(
    mol,
    *,
    forcefield: str = "mmff",
    max_iters: int = 200,
):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work_mol = Chem.Mol(mol)
    if work_mol.GetNumConformers() == 0:
        work_mol = generate_ligand_3d(work_mol, add_hs=True, optimize=False)
    normalized_forcefield = str(forcefield or "mmff").strip().lower()
    if normalized_forcefield == "mmff" and AllChem.MMFFHasAllMoleculeParams(work_mol):
        AllChem.MMFFOptimizeMolecule(work_mol, maxIters=int(max_iters))
    else:
        AllChem.UFFOptimizeMolecule(work_mol, maxIters=int(max_iters))
    return work_mol


__all__ = [
    "generate_ligand_3d",
    "minimize_ligand_molecule",
    "protonate_ligand_molecule",
    "standardize_ligand_molecule",
]
