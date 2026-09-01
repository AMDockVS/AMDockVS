"""The import decision is pure: in goes a `Mol`, out comes the decision — without touching disk.

What is protected here and no import test covers: that a late rejection (the property gate,
which runs *after* the prep) still costs zero files written.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("rdkit")

from rdkit import Chem

from amdockvs.io.rows import active_small_molecule_criteria, evaluate_ligand_mol, ligand_row_fields
from amdockvs.io.import_stats import FILTERED_PROPERTY, NO_VALID_FRAGMENT
from amdockvs.io.payloads import ImportPrefilterPolicy
from amdockvs.models.molecules import MoleculeType


def _common(root: Path) -> dict:
    return {"storage_root": root / "store", "role": "ligand", "storage_key": "t", "project_root": root}


def test_a_salt_is_split_and_nothing_is_written(tmp_path):
    decision, reason = evaluate_ligand_mol(
        mol=Chem.MolFromSmiles("CCO.[Na+]"), metadata={}, **_common(tmp_path)
    )
    assert reason is None and decision is not None
    assert Chem.MolToSmiles(decision.kept_mol) == "CCO"
    assert not any(tmp_path.rglob("*.sdf")), "evaluating must write nothing"

    row = ligand_row_fields({}, decision, current_path_rel="data/x.sdf")
    assert row["current_path"] == "data/x.sdf" and row["n_atoms"] == 3
    assert row["extra_data"]["fragmentation"] is decision.fragment_info


def test_a_molecule_without_any_valid_fragment_is_rejected_with_a_reason(tmp_path):
    decision, reason = evaluate_ligand_mol(mol=Chem.MolFromSmiles(""), metadata={}, **_common(tmp_path))
    assert decision is None and reason == NO_VALID_FRAGMENT


def test_a_late_property_rejection_still_costs_zero_files(tmp_path):
    strict = ImportPrefilterPolicy(max_heavy_atoms=1)
    decision, reason = evaluate_ligand_mol(
        mol=Chem.MolFromSmiles("CCO"),
        metadata={},
        prefilter=strict,
        criteria=active_small_molecule_criteria(strict, MoleculeType.SMALL_MOLECULE),
        molecule_kind=MoleculeType.SMALL_MOLECULE,
        **_common(tmp_path),
    )
    assert decision is None and reason == FILTERED_PROPERTY
    assert not any(tmp_path.rglob("*.sdf"))


def test_the_same_criteria_loosened_lets_the_same_molecule_through(tmp_path):
    loose = ImportPrefilterPolicy(max_heavy_atoms=50)
    decision, reason = evaluate_ligand_mol(
        mol=Chem.MolFromSmiles("CCO"),
        metadata={},
        prefilter=loose,
        criteria=active_small_molecule_criteria(loose, MoleculeType.SMALL_MOLECULE),
        molecule_kind=MoleculeType.SMALL_MOLECULE,
        **_common(tmp_path),
    )
    assert decision is not None and reason is None


def test_the_optional_prep_runs_before_the_gate_and_lands_in_the_row(tmp_path):
    decision, reason = evaluate_ligand_mol(
        mol=Chem.MolFromSmiles("CCO"),
        metadata={},
        prefilter=ImportPrefilterPolicy(add_hs=True),
        **_common(tmp_path),
    )
    assert reason is None and decision.prepped_state is not None
    assert ligand_row_fields({}, decision, current_path_rel="x.sdf")["has_hs"] is True
