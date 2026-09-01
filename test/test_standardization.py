"""The standardisation core: in goes a mol, out comes a mol. No files, no sessions.

`changed` is what decides whether the molecule file has to be rewritten, so the no-op cases
matter as much as the transforming ones.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from rdkit import Chem

from amdockvs.chemistry.standardization import (
    add_explicit_hydrogens,
    prepare_import_structure,
    remove_explicit_hydrogens,
    standardize_smiles,
)
from amdockvs.chemistry.state import mol_has_3d, mol_has_explicit_hs


def test_hydrogens_round_trip():
    ethanol = Chem.MolFromSmiles("CCO")
    with_hs = add_explicit_hydrogens(ethanol)
    assert with_hs.GetNumAtoms() == 9
    assert remove_explicit_hydrogens(with_hs).GetNumAtoms() == 3


def test_asking_for_nothing_leaves_the_molecule_untouched():
    ethanol = Chem.MolFromSmiles("CCO")
    same, changed = prepare_import_structure(ethanol, add_hs=False, gen_3d=False, canonical_tautomer=False)
    assert same is ethanol and changed is False


def test_add_hs_is_idempotent():
    hs_mol, changed = prepare_import_structure(
        Chem.MolFromSmiles("CCO"), add_hs=True, gen_3d=False, canonical_tautomer=False
    )
    assert changed is True and mol_has_explicit_hs(hs_mol)
    again, changed = prepare_import_structure(hs_mol, add_hs=True, gen_3d=False, canonical_tautomer=False)
    assert again is hs_mol and changed is False


def test_gen_3d_also_satisfies_add_hs_and_is_idempotent():
    embedded, changed = prepare_import_structure(
        Chem.MolFromSmiles("CCO"), add_hs=False, gen_3d=True, canonical_tautomer=False
    )
    assert changed is True and mol_has_3d(embedded) and mol_has_explicit_hs(embedded)
    reused, changed = prepare_import_structure(embedded, add_hs=False, gen_3d=True, canonical_tautomer=False)
    assert reused is embedded and changed is False


def test_canonical_tautomer():
    tautomer, changed = prepare_import_structure(
        Chem.MolFromSmiles("CC(=O)CC(=O)C"), add_hs=False, gen_3d=False, canonical_tautomer=True
    )
    assert changed is True and Chem.MolToSmiles(tautomer) == "CC(=O)CC(C)=O"


def test_standardize_smiles_drops_the_salt_and_the_charge():
    assert standardize_smiles("CC(=O)[O-].[Na+]") == "CC(=O)O"
    assert (
        standardize_smiles("CC(=O)[O-].[Na+]", fragment_parent=False, neutralize=False)
        == "CC(=O)[O-].[Na+]"
    )


def test_an_invalid_smiles_raises_instead_of_returning_garbage():
    with pytest.raises(ValueError):
        standardize_smiles("not-a-smiles")
