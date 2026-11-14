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
        "extra_data": {},
        "has_3d": False,
    }


def test_multi_step_rows_return_one_payload_and_report_the_folded_state():
    from ms_flow.payloads import Payload

    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(
        operations=[("standardize", {}), ("protonate", {"method": "explicit_hs"}), ("generate_3d", {})],
        rows=[_row(index, smiles) for index, smiles in enumerate(SMILES, start=1)],
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
    )

    update, = result["updates"]
    assert len(update["model_rows"]) == update["state"]["conformer_count"] > 1
    assert update["state"]["is_minimized"] is True
    # Every conformer is its own payload and the first one is the molecule's current structure.
    assert len({id(row["payload_id"]) for row in update["model_rows"]}) == len(update["model_rows"])
    assert update["current_payload_id"] is update["model_rows"][0]["payload_id"]


def _is_3d(payload) -> bool:
    mol = Chem.MolFromMolBlock(payload.data.decode(), removeHs=False)
    z = [mol.GetConformer().GetAtomPosition(i).z for i in range(mol.GetNumAtoms())]
    return mol.GetConformer().Is3D() and max(z) - min(z) > 0.1


def test_protonating_a_2d_structure_keeps_it_2d():
    from amdockvs.chemistry.service import transform_ligand_rows

    # The row's structure is a flat 2D SDF, as left by an earlier 2D-only Build pass.
    result = transform_ligand_rows(operations=[("protonate", {"method": "explicit_hs"})], rows=[_row(1, "CCO")])

    update, = result["updates"]
    assert update["state"]["has_3d"] is False
    assert not _is_3d(update["current_payload_id"])


def test_minimizing_a_2d_structure_embeds_it_in_3d_first():
    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(operations=[("minimize", {})], rows=[_row(1, "CCCCCCO")])

    update, = result["updates"]
    assert update["state"]["has_3d"] is True
    assert update["state"]["is_minimized"] is True
    assert _is_3d(update["current_payload_id"])


def test_a_broken_row_does_not_take_the_batch_down():
    from amdockvs.chemistry.service import transform_ligand_rows

    result = transform_ligand_rows(
        operations="standardize",
        rows=[_row(1, None), _row(2, SMILES[0])],
    )

    assert result["failure_count"] == 1
    assert result["failure_samples"][0]["entity_id"] == 1
    assert [update["entity_id"] for update in result["updates"]] == [2]


def test_a_failed_molecule_is_excluded_with_the_step_it_failed_at():
    from amdockvs.chemistry.tasks import ligand_chemistry_task

    rows = [_row(1, SMILES[0]), _row(2, SMILES[1])]
    rows[1]["structure"] = Chem.MolToMolBlock(Chem.Mol()).encode()  # no atoms: 3D cannot embed it
    payload = ligand_chemistry_task({"operation": [["standardize", {}], ["generate_3d", {}]], "rows": rows})

    assert [row["id"] for row in payload["molecules"]] == [1]
    [failed] = payload["molecule_failures"]
    assert failed["id"] == 2 and failed["excluded"] is True
    assert failed["exclusion_reason"].split(":")[0] in {"standardize", "generate_3d"}


def test_3d_builds_fused_rings_etkdg_rejects_and_refuses_impossible_stereo():
    buildable = Chem.MolFromSmiles("O[C@@H]1[C@H]2CNC[C@H]1OC2")
    built = generate_ligand_3d(buildable)
    Chem.AssignStereochemistryFrom3D(built)
    assert Chem.MolToSmiles(Chem.RemoveHs(built)) == Chem.MolToSmiles(buildable)

    try:
        generate_ligand_3d(Chem.MolFromSmiles("CS(=O)(=O)N1C[C@@H]2C[C@@]2(N)C1"))  # trans-fused cyclopropane
    except ValueError as exc:
        assert "stereochemistry" in str(exc)
    else:
        raise AssertionError("an impossible ring fusion must not get coordinates")

def _api_without_a_project():
    from types import SimpleNamespace

    from amdockvs.chemistry.api import ChemistryAPI

    def no_submission():
        raise AssertionError("an invalid force field must be refused before anything is submitted")

    return ChemistryAPI(SimpleNamespace(_require_active_project=no_submission))


def test_an_unknown_force_field_is_refused_before_submission():
    import pytest

    api = _api_without_a_project()
    with pytest.raises(ValueError, match="mmff_typo"):
        api.minimize_ligands(forcefield="mmff_typo")
    for submit in (api.run_ligand_pipeline, api.run_shard_pipeline):
        with pytest.raises(ValueError, match="mmff_typo"):
            submit([("generate_3d", {}), ("minimize", {"forcefield": "mmff_typo"})])


def test_an_unknown_force_field_never_falls_back_to_uff_in_the_worker():
    import pytest

    from amdockvs.chemistry.tools import minimize_ligand_molecule

    mol = generate_ligand_3d(Chem.MolFromSmiles("CCO"))
    with pytest.raises(ValueError, match="mmff_typo"):
        minimize_ligand_molecule(mol, forcefield="mmff_typo")
    # The names the Build dropdown offers still work, in any case.
    for name in ("mmff", "UFF", " Mmff "):
        assert minimize_ligand_molecule(mol, forcefield=name).GetNumConformers() == 1

def test_build_writes_the_built_structures_descriptors_back_to_the_row():
    from amdockvs.chemistry.descriptors import BASIC_DESCRIPTOR_FIELDS, calculate_basic_descriptors
    from amdockvs.chemistry.jobs import CHEMISTRY_GRAPH_OUTPUT
    from amdockvs.chemistry.service import transform_ligand_rows
    from amdockvs.chemistry.tasks import _chemistry_graph_payload

    # The sink's column list is this tuple: a key it misses would be silently dropped.
    assert set(BASIC_DESCRIPTOR_FIELDS) == set(calculate_basic_descriptors(Chem.MolFromSmiles("CCO")))

    # Standardize neutralizes the imported acetate: charge -1 -> 0; then explicit Hs.
    result = transform_ligand_rows(
        operations=[("standardize", {}), ("protonate", {"method": "explicit_hs"})],
        rows=[_row(1, "CC(=O)[O-]")],
    )

    molecule, = _chemistry_graph_payload(result["updates"], [])["molecules"]
    assert molecule["formal_charge"] == 0
    assert molecule["n_atoms"] == 8
    assert set(BASIC_DESCRIPTOR_FIELDS) | {"n_atoms"} <= set(molecule)
    nodes = CHEMISTRY_GRAPH_OUTPUT.meta["graph"]["nodes"]
    columns = next(node for node in nodes if node["name"] == "molecules")["columns"]
    assert set(molecule) <= set(columns)
