"""`split_fragments`: the co-components of a record become molecules of their own.

Worth a test because the rule is "distinct organic fragments, minus the one already kept" —
getting it wrong either duplicates every ligand or silently imports chlorides as candidates.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Chem = pytest.importorskip("rdkit.Chem")

from amdockvs.molecules.fragments import extra_fragment_molecules
from amdockvs.core.paths import molecule_storage_key


def _smiles(extras):
    return sorted(Chem.MolToSmiles(mol) for _index, mol in extras)


def test_a_single_fragment_record_yields_nothing_extra():
    assert extra_fragment_molecules(Chem.MolFromSmiles("c1ccccc1O")) == []


def test_two_real_fragments_yield_the_one_that_is_not_kept():
    pair = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O")  # both drug-sized
    assert _smiles(extra_fragment_molecules(pair)) == ["Oc1cccc2ccccc12"]


def test_a_counterion_is_not_a_molecule_of_its_own():
    # Acetate next to a drug-sized fragment: organic, but a fraction of its size.
    salt = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.CC(=O)[O-]")
    assert extra_fragment_molecules(salt) == []


def test_ions_and_repeats_are_skipped():
    mol = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.[Cl-].[Cl-].c1ccc2ccccc2c1O.c1ccc2ccccc2c1O")
    assert _smiles(extra_fragment_molecules(mol)) == ["Oc1cccc2ccccc12"]


def test_each_extra_gets_its_own_storage_key():
    base = molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7)
    assert molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7, "frag01") != base
    assert molecule_storage_key("ligand", Path("/tmp/lib.sdf"), 7) == base  # unchanged without a variant


def test_split_fragments_materializes_the_co_component_as_its_own_row(tmp_path):
    """End-to-end through the materializer: one record in, two molecules out."""
    from amdockvs.io.transformers.materializers import materialize_import_batch

    mol = Chem.MolFromSmiles("c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O")
    mol.SetProp("_Name", "pair")
    record = Chem.MolToMolBlock(mol) + "$$$$\n"
    sdf = tmp_path / "ligands.sdf"
    sdf.write_text(record, encoding="utf-8")
    payload = {
        "kind": "ligand",
        "file_path": str(sdf),
        "storage_dir": str(tmp_path / "data" / "ligands"),
        "input_format": "sdf",
        "primary_role": "ligand",
        "molecule_kind": "small_molecule",
        "prefilter": {"target_molecule_kinds": ["small_molecule"], "split_fragments": True},
        "entries": [{"source_index": 0, "raw": record}],
    }

    rows = materialize_import_batch(payload)
    assert [row["name"] for row in rows] == ["pair", "pair [frag02]"]
    assert len({row["current_path"] for row in rows}) == 2  # separate files, not one overwritten
    # The extra points back at the record's own molecule, by the index its parent lists it under.
    link = rows[1]["extra_data"]["split_from"]
    assert link["parent_name"] == "pair" and link["source_index"] == 0
    parent_components = rows[0]["extra_data"]["fragmentation"]["components"]
    assert link["fragment_index"] in {c["fragment_index"] for c in parent_components}

    payload["prefilter"] = {"target_molecule_kinds": ["small_molecule"]}
    assert [row["name"] for row in materialize_import_batch(payload)] == ["pair"]
