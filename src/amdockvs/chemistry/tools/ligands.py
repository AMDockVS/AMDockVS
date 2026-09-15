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
    random_seed: int = 0xF00D,
    method: str = "etkdgv3",
    attempts: int = 1,
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
    # ETKDG needs explicit Hs; AddHs only fills implicit ones, so protonated Hs are kept.
    work_mol = Chem.AddHs(work_mol)
    presets = {"etkdgv3": AllChem.ETKDGv3, "sretkdgv3": AllChem.srETKDGv3}
    preset = presets.get(str(method or "etkdgv3").strip().lower())
    if preset is None:
        raise ValueError(f"Unsupported 3D embedding method: {method}")
    params = preset()
    params.randomSeed = int(random_seed)
    params.numThreads = 1  # the executor already parallelizes by process
    num_confs = max(1, int(attempts))
    conf_ids = list(AllChem.EmbedMultipleConfs(work_mol, numConfs=num_confs, params=params))
    if not conf_ids:
        # Random starting coordinates rescue large molecules and macrocycles the default start fails on.
        params.useRandomCoords = True
        conf_ids = list(AllChem.EmbedMultipleConfs(work_mol, numConfs=num_confs, params=params))
    if not conf_ids:
        raise ValueError("RDKit could not generate a 3D conformer.")
    minimized = False
    if len(conf_ids) > 1:
        # Best of N: raw embeddings have no comparable energy, so each attempt is optimized first.
        results = None
        if AllChem.MMFFHasAllMoleculeParams(work_mol):
            results = AllChem.MMFFOptimizeMoleculeConfs(work_mol, numThreads=1)
        elif AllChem.UFFHasAllMoleculeParams(work_mol):
            results = AllChem.UFFOptimizeMoleculeConfs(work_mol, numThreads=1)
        best = 0
        if results:
            minimized = True
            best = min(range(len(results)), key=lambda index: results[index][1])
        keep = Chem.Conformer(work_mol.GetConformer(conf_ids[best]))
        work_mol.RemoveAllConformers()
        work_mol.AddConformer(keep, assignId=True)
    work_mol.SetBoolProp("_amdock_is_minimized", minimized)
    return work_mol


def minimize_ligand_molecule(
    mol,
    *,
    forcefield: str = "mmff",
    max_iters: int = 200,
    prune_rms_thresh: float = 0.0,
):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work_mol = Chem.Mol(mol)
    if work_mol.GetNumConformers() == 0:
        work_mol = generate_ligand_3d(work_mol)
    normalized_forcefield = str(forcefield or "mmff").strip().lower()
    # Every conformer, so an ensemble minimizes as a whole; a single conformer is the same call.
    if normalized_forcefield == "mmff" and AllChem.MMFFHasAllMoleculeParams(work_mol):
        results = AllChem.MMFFOptimizeMoleculeConfs(work_mol, numThreads=1, maxIters=int(max_iters))
    else:
        results = AllChem.UFFOptimizeMoleculeConfs(work_mol, numThreads=1, maxIters=int(max_iters))
    if float(prune_rms_thresh) > 0 and work_mol.GetNumConformers() > 1:
        work_mol = _prune_minimized_conformers(
            work_mol, [energy for _not_converged, energy in results], float(prune_rms_thresh)
        )
    return work_mol


def _prune_minimized_conformers(mol, energies, threshold: float):
    """Minimization can drop neighbouring conformers into one basin: keep the lowest-energy one.

    Survivors come out sorted by energy, so the first conformer — the current model — is the lowest.
    """
    from rdkit import Chem
    from rdkit.Chem import rdMolAlign

    heavy = Chem.RemoveHs(mol)
    conformers = list(mol.GetConformers())
    kept: list[int] = []
    # ponytail: O(n²) symmetry-aware alignments; AllChem.GetConformerRMSMatrix if big ensembles get slow.
    for index in sorted(range(len(conformers)), key=energies.__getitem__):
        conf_id = conformers[index].GetId()
        if all(rdMolAlign.GetBestRMS(heavy, heavy, conf_id, kept_id) >= threshold for kept_id in kept):
            kept.append(conf_id)
    survivors = [Chem.Conformer(mol.GetConformer(conf_id)) for conf_id in kept]
    mol.RemoveAllConformers()
    for conformer in survivors:
        mol.AddConformer(conformer, assignId=True)
    return mol


__all__ = [
    "generate_ligand_3d",
    "minimize_ligand_molecule",
    "protonate_ligand_molecule",
    "standardize_ligand_molecule",
]
