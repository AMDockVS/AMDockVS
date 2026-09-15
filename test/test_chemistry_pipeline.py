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


def _row(molecule_id: int, source):
    return {
        "id": molecule_id,
        "stored_path": str(source),
        "current_path": str(source),
        "current_model_index": None,
        "extra_data": {},
        "has_3d": False,
    }


def test_multi_step_rows_write_one_file_and_report_the_folded_state(tmp_path):
    from amdockvs.chemistry.service import transform_ligand_rows

    project_root = tmp_path / "project"
    output_dir = project_root / "data" / "molecules"
    artifacts = output_dir / "original"
    artifacts.mkdir(parents=True)
    sources = []
    for index, smiles in enumerate(SMILES, start=1):
        path = artifacts / f"lig{index}.sdf"
        path.write_text(Chem.MolToMolBlock(Chem.MolFromSmiles(smiles)), encoding="utf-8")
        sources.append(path)

    result = transform_ligand_rows(
        operations=[("standardize", {}), ("protonate", {"method": "explicit_hs"}), ("generate_3d", {})],
        output_dir=output_dir,
        rows=[_row(index, path) for index, path in enumerate(sources, start=1)],
        params={"structure_source": "current", "run_id": "multi"},
        next_model_index_by_entity={1: 0, 2: 0, 3: 0},
    )

    assert result["failure_count"] == 0
    assert len(result["updates"]) == len(SMILES)
    for update in result["updates"]:
        # generate_3d ran last: one model row, coordinates, and the H's protonate added.
        assert len(update["model_rows"]) == 1
        assert update["state"]["has_3d"] is True
        assert update["state"]["has_hs"] is True
        assert (project_root / update["current_path"]).is_file()
    # One pass, one artifact per ligand — not one per step.
    written = [path for path in output_dir.rglob("*.sdf") if path.parent != artifacts]
    assert len(written) == len(SMILES)


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


def test_minimize_after_conformers_still_writes_the_ensemble(tmp_path):
    from amdockvs.chemistry.service import transform_ligand_rows

    output_dir = tmp_path / "project" / "data" / "molecules"
    source = output_dir / "original" / "lig.sdf"
    source.parent.mkdir(parents=True)
    source.write_text(Chem.MolToMolBlock(Chem.MolFromSmiles("CCCCCCO")), encoding="utf-8")

    result = transform_ligand_rows(
        operations=[
            ("generate_3d", {}),
            ("conformers", {"num_conformers": 4, "prune_rms_thresh": 0.0, "optimize": False}),
            ("minimize", {}),
        ],
        output_dir=output_dir,
        rows=[_row(1, source)],
        params={"structure_source": "current", "run_id": "ens"},
        next_model_index_by_entity={1: 0},
    )

    update, = result["updates"]
    assert len(update["model_rows"]) == update["state"]["conformer_count"] > 1
    assert update["state"]["is_minimized"] is True


def test_a_broken_row_does_not_take_the_batch_down(tmp_path):
    from amdockvs.chemistry.service import transform_ligand_rows

    project_root = tmp_path / "project"
    output_dir = project_root / "data" / "molecules"
    artifacts = output_dir / "original"
    artifacts.mkdir(parents=True)
    good = artifacts / "good.sdf"
    good.write_text(Chem.MolToMolBlock(Chem.MolFromSmiles(SMILES[0])), encoding="utf-8")

    result = transform_ligand_rows(
        operations="standardize",
        output_dir=output_dir,
        rows=[_row(1, artifacts / "missing.sdf"), _row(2, good)],
        params={"structure_source": "current", "run_id": "partial"},
    )

    assert result["failure_count"] == 1
    assert result["failure_samples"][0]["entity_id"] == 1
    assert [update["entity_id"] for update in result["updates"]] == [2]
