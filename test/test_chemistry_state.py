import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "//src")


def test_molecule_state_metadata_reports_2d_molecule_without_explicit_hydrogens():
    pytest.importorskip("rdkit")

    from rdkit import Chem

    from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs, molecule_state_metadata

    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None

    assert mol_has_3d(mol) is False
    assert mol_has_explicit_hs(mol) is False
    assert molecule_state_metadata(mol) == {
        "state": {
            "has_3d": False,
            "has_hs": False,
            "conformer_count": 0,
        }
    }


def test_molecule_state_metadata_reports_3d_molecule_with_explicit_hydrogens():
    pytest.importorskip("rdkit")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs, molecule_state_metadata

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert mol is not None
    assert AllChem.EmbedMolecule(mol, randomSeed=0xF00D) == 0

    assert mol_has_3d(mol) is True
    assert mol_has_explicit_hs(mol) is True

    state = molecule_state_metadata(mol)
    assert state["state"]["has_3d"] is True
    assert state["state"]["has_hs"] is True
    assert state["state"]["conformer_count"] == 1
