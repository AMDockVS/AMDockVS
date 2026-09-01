import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")


def test_redocking_rmsd_does_not_realign_pose(tmp_path):
    pytest.importorskip("rdkit")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.docking.rmsd import pose_rmsd_vs_reference

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0
    ref = Chem.RemoveHs(mol, sanitize=False)
    pose = Chem.Mol(ref)
    conf = pose.GetConformer()
    for atom_index in range(pose.GetNumAtoms()):
        point = conf.GetAtomPosition(atom_index)
        conf.SetAtomPosition(atom_index, (point.x + 10.0, point.y, point.z))

    ref_path = tmp_path / "reference.sdf"
    pose_path = tmp_path / "pose.sdf"
    writer = Chem.SDWriter(str(ref_path))
    writer.write(ref)
    writer.close()
    writer = Chem.SDWriter(str(pose_path))
    writer.write(pose)
    writer.close()

    rmsd = pose_rmsd_vs_reference(reference_ligand_path=ref_path, pose_path=pose_path)

    assert rmsd is not None
    assert rmsd > 9.0


def test_redocking_rmsd_keeps_identical_pose_near_zero(tmp_path):
    pytest.importorskip("rdkit")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.docking.rmsd import pose_rmsd_vs_reference

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert AllChem.EmbedMolecule(mol, randomSeed=11) == 0
    ref = Chem.RemoveHs(mol, sanitize=False)

    ref_path = tmp_path / "reference.sdf"
    pose_path = tmp_path / "pose.sdf"
    writer = Chem.SDWriter(str(ref_path))
    writer.write(ref)
    writer.close()
    writer = Chem.SDWriter(str(pose_path))
    writer.write(ref)
    writer.close()

    rmsd = pose_rmsd_vs_reference(reference_ligand_path=ref_path, pose_path=pose_path)

    assert rmsd is not None
    assert rmsd < 1.0e-6

    from amdockvs.docking.rmsd import pose_rmsd_detail

    detail = pose_rmsd_detail(reference_ligand_path=ref_path, pose_path=pose_path)
    assert detail is not None
    value, method = detail
    assert value < 1.0e-6
    assert method in {"substruct", "ordered"}
