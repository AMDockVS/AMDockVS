"""A boron docks as a marked carbon (AutoDock/Vina have no boron type) and comes back a boron in the pose."""
from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import AllChem

from amdockvs.docking.preparation.meeko import boron_in_sd, prepare_ligand_vina_pdbqt_from_mol
from amdockvs.io.formats import read_mol


def _ligand(smiles: str):
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    return mol


def test_a_boron_is_typed_as_carbon_and_read_back_as_boron(tmp_path):
    for smiles in ("OB(O)c1ccccc1", "F[B-](F)(F)F", "O=BOB(O)OB(O)OC=O"):
        pdbqt = str(prepare_ligand_vina_pdbqt_from_mol(_ligand(smiles)).payload)
        atom_types = {line.split()[-1] for line in pdbqt.splitlines() if line.startswith(("ATOM", "HETATM"))}
        assert not atom_types & {"B", "Bx"}  # nothing Vina cannot type
        path = tmp_path / "lig.pdbqt"
        path.write_text(pdbqt)
        back = Chem.MolFromSmiles(Chem.MolToSmiles(read_mol(path)))  # re-parsed: implicit Hs again
        assert Chem.MolToSmiles(back) == Chem.MolToSmiles(Chem.MolFromSmiles(smiles))


def test_a_pose_sd_gets_its_borons_back():
    from meeko import PDBQTMolecule, RDKitMolCreate

    pdbqt = str(prepare_ligand_vina_pdbqt_from_mol(_ligand("OB(O)c1ccccc1")).payload)
    sd, _ = RDKitMolCreate.write_sd_string(PDBQTMolecule(pdbqt, skip_typing=True))
    supplier = Chem.SDMolSupplier()
    supplier.SetData(boron_in_sd(sd))
    (mol,) = list(supplier)
    assert Chem.MolToSmiles(Chem.MolFromSmiles(Chem.MolToSmiles(mol))) == "OB(O)c1ccccc1"
