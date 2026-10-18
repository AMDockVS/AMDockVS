"""The pipeline must equal the hand-chained calls, and must not lose anyone's position."""

from rdkit import Chem

from amdockvs.chemistry.pipeline import LIGAND_STEPS, normalize_steps, run_pipeline
from amdockvs.chemistry.protonation import protonate_molecule_batch
from amdockvs.chemistry.tools import generate_ligand_3d, standardize_ligand_molecule

SMILES = ["CC(=O)Oc1ccccc1C(=O)O", "CCN(CC)CC", "c1ccccc1O"]


def _mols():
    return [Chem.MolFromSmiles(smiles) for smiles in SMILES]


def test_pipeline_matches_chained_calls():
    steps = [("standardize", {}), ("protonate", {"method": "dimorphite"}), ("generate_3d", {})]
    piped = run_pipeline(_mols(), steps)

    standardized = [standardize_ligand_molecule(mol) for mol in _mols()]
    protonated = protonate_molecule_batch(
        list(enumerate(standardized, start=1)), method="dimorphite", params={}
    )
    chained = [generate_ligand_3d(protonated[index]) for index in range(1, len(standardized) + 1)]

    assert [Chem.MolToSmiles(mol) for mol in piped] == [Chem.MolToSmiles(mol) for mol in chained]


def test_failure_keeps_its_slot_and_the_rest_go_through():
    batch = [_mols()[0], Chem.Mol(), _mols()[2]]  # an empty Mol has no atoms to embed
    out = run_pipeline(batch, [("generate_3d", {})])

    assert len(out) == 3
    assert isinstance(out[1], Exception)
    assert all(out[i].GetNumConformers() == 1 for i in (0, 2))


def test_exceptions_pass_through_later_steps_untouched():
    failure = ValueError("boom")
    out = run_pipeline([failure, _mols()[0]], [("standardize", {}), ("generate_3d", {})])

    assert out[0] is failure
    assert out[1].GetNumConformers() == 1


def test_normalize_steps_accepts_a_bare_name():
    assert normalize_steps("Protonate") == [("protonate", {})]
    assert normalize_steps([("minimize", {"max_iters": 5})]) == [("minimize", {"max_iters": 5})]
    assert normalize_steps(list(LIGAND_STEPS)) == [(name, {}) for name in LIGAND_STEPS]


def test_unknown_step_is_rejected():
    try:
        run_pipeline(_mols(), [("teleport", {})])
    except ValueError as exc:
        assert "teleport" in str(exc)
    else:
        raise AssertionError("an unknown operation must not pass silently")


def _row(molecule_id: int, smiles: str | None):
    """What the job feed hands the task: the row with the bytes of its structure."""
    molblock = None if smiles is None else Chem.MolToMolBlock(Chem.MolFromSmiles(smiles))
    return {
        "id": molecule_id,
        "structure": None if molblock is None else molblock.encode(),
        "structure_kind": "sdf",
        "current_model_index": None,
        "extra_data": {},
        "has_3d": False,
    }


def test_multi_step_rows_return_one_payload_and_report_the_folded_state():
    from ms_flow.payloads import Payload

    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(
        operations=[("standardize", {}), ("protonate", {"method": "explicit_hs"}), ("generate_3d", {})],
        rows=[_row(index, smiles) for index, smiles in enumerate(SMILES, start=1)],
        next_model_index_by_entity={1: 0, 2: 0, 3: 0},
    )

    assert result["failure_count"] == 0
    assert len(result["updates"]) == len(SMILES)
    for update in result["updates"]:
        # generate_3d ran last: one model row, coordinates, and the H's protonate added.
        assert len(update["model_rows"]) == 1
        assert update["state"]["has_3d"] is True
        assert update["state"]["has_hs"] is True
        # One pass, one structure per ligand — not one per step — and the model is that structure.
        current = update["current_payload_id"]
        assert isinstance(current, Payload) and (current.kind, current.series) == ("sdf", "molecules")
        assert update["model_rows"][0]["payload_id"] is current
        assert Chem.MolFromMolBlock(current.data.decode(), removeHs=False).GetNumConformers() == 1


def _mmff_energies(mol):
    from rdkit.Chem import AllChem

    props = AllChem.MMFFGetMoleculeProperties(mol)
    return [
        AllChem.MMFFGetMoleculeForceField(mol, props, confId=conformer.GetId()).CalcEnergy()
        for conformer in mol.GetConformers()
    ]


def test_minimize_optimizes_every_conformer_of_an_ensemble():
    steps = [("conformers", {"num_conformers": 4, "prune_rms_thresh": 0.0, "optimize": False})]
    ensemble = run_pipeline([Chem.MolFromSmiles("CCCCCCO")], steps)[0]

    before = _mmff_energies(ensemble)
    after = _mmff_energies(run_pipeline([ensemble], [("minimize", {})])[0])

    assert len(after) == len(before) > 1
    assert all(minimized < raw for minimized, raw in zip(after, before))


def test_minimize_prunes_conformers_that_converged_and_sorts_by_energy():
    from rdkit.Chem import rdMolAlign

    # From measurement: 27 distinct ETKDG conformers of this molecule collapse to 5-6 duplicate pairs under MMFF.
    steps = [("conformers", {"num_conformers": 30, "random_seed": 42, "prune_rms_thresh": 0.5, "optimize": False})]
    ensemble = run_pipeline([Chem.MolFromSmiles("OC1CCN(CC1)C1CCC(CC1)C(=O)N1CCCC1")], steps)[0]

    pruned = run_pipeline([ensemble], [("minimize", {"prune_rms_thresh": 0.5})])[0]

    heavy = Chem.RemoveHs(pruned)
    ids = [conformer.GetId() for conformer in heavy.GetConformers()]
    assert 1 < len(ids) < ensemble.GetNumConformers()
    assert all(rdMolAlign.GetBestRMS(heavy, heavy, a, b) >= 0.5 for i, a in enumerate(ids) for b in ids[:i])
    energies = _mmff_energies(pruned)
    assert energies == sorted(energies)


def test_minimize_after_conformers_still_writes_the_ensemble():
    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(
        operations=[
            ("generate_3d", {}),
            ("conformers", {"num_conformers": 4, "prune_rms_thresh": 0.0, "optimize": False}),
            ("minimize", {}),
        ],
        rows=[_row(1, "CCCCCCO")],
        next_model_index_by_entity={1: 0},
    )

    update, = result["updates"]
    assert len(update["model_rows"]) == update["state"]["conformer_count"] > 1
    assert update["state"]["is_minimized"] is True
    # Every conformer is its own payload and the first one is the molecule's current structure.
    assert len({id(row["payload_id"]) for row in update["model_rows"]}) == len(update["model_rows"])
    assert update["current_payload_id"] is update["model_rows"][0]["payload_id"]
    assert update["current_model_index"] == 0


def test_a_broken_row_does_not_take_the_batch_down():
    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(
        operations="standardize",
        rows=[_row(1, None), _row(2, SMILES[0])],
    )

    assert result["failure_count"] == 1
    assert result["failure_samples"][0]["entity_id"] == 1
    assert [update["entity_id"] for update in result["updates"]] == [2]
