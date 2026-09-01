from __future__ import annotations


def generate_3d_molecule(
    mol,
    *,
    add_hs: bool = True,
    random_seed: int = 0xF00D,
    optimize: bool = True,
):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work_mol = Chem.Mol(mol)
    if add_hs:
        work_mol = Chem.AddHs(work_mol)

    params = AllChem.ETKDGv3()
    params.randomSeed = int(random_seed)
    conf_id = AllChem.EmbedMolecule(work_mol, params)
    if conf_id < 0:
        raise ValueError("RDKit could not generate a 3D conformer.")
    if optimize:
        AllChem.UFFOptimizeMolecule(work_mol, confId=conf_id)
    return work_mol


def generate_conformer_ensemble(
    mol,
    *,
    num_conformers: int = 20,
    add_hs: bool = True,
    random_seed: int = 0xF00D,
    prune_rms_thresh: float = 0.5,
    optimize: bool = True,
):
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work_mol = Chem.Mol(mol)
    if add_hs:
        work_mol = Chem.AddHs(work_mol)

    params = AllChem.ETKDGv3()
    params.randomSeed = int(random_seed)
    params.pruneRmsThresh = float(prune_rms_thresh)
    conf_ids = list(AllChem.EmbedMultipleConfs(work_mol, numConfs=int(num_conformers), params=params))
    if not conf_ids:
        raise ValueError("RDKit could not generate any conformers.")
    if optimize:
        for conf_id in conf_ids:
            AllChem.UFFOptimizeMolecule(work_mol, confId=int(conf_id))
    return work_mol, conf_ids


__all__ = ["generate_3d_molecule", "generate_conformer_ensemble"]
