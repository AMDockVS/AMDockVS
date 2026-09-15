"""A shard carries properties, so identity survives the pipeline without a row to keep it in.

The container stores raw record bytes with a kind tag, and what the chemistry pipeline emits is
always SDF: title line plus SD tags. So `_Name` and every property written before the step are
still there after it. The one format with nowhere to put them is SMILES on the way in — a `.smi`
line is `SMILES<space>name` and nothing else — which is the only place identity needs help.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rdkit")

from amdockvs.chemistry.shards import _molblock
from amdockvs.io.shards import mol_from_record


def test_a_smiles_line_keeps_its_name():
    assert mol_from_record("CCO ZINC000123", "smiles").GetProp("_Name") == "ZINC000123"
    assert mol_from_record("CCO", "smiles").GetNumAtoms() == 3  # nameless is still a molecule
    assert mol_from_record("   ", "smiles") is None


def test_properties_survive_a_round_trip_through_a_shard_record():
    mol = mol_from_record("CCO ZINC000123", "smiles")
    mol.SetProp("activity", "7.2")
    back = mol_from_record(_molblock(mol), "sdf")
    assert back.GetProp("_Name") == "ZINC000123"
    assert back.GetProp("activity") == "7.2"
