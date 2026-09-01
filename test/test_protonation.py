from pathlib import Path

from rdkit import Chem

from amdockvs.chemistry.protonation import protonate_molecule_batch
from amdockvs.chemistry.jobs import LigandChemistryJobParams, _iter_ligand_chemistry_batches
from amdockvs.chemistry.service import ligand_working_path, transform_ligand_rows


def _write_mol(path: Path, smiles: str) -> None:
    molecule = Chem.MolFromSmiles(smiles)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Chem.MolToMolBlock(molecule), encoding="utf-8")


def test_polar_hydrogens_keeps_only_heteroatom_hydrogens():
    source = Chem.MolFromSmiles("CCO")

    result = protonate_molecule_batch([(7, source)], method="polar_hydrogens", params={})[7]

    explicit_h_parents = {
        neighbor.GetAtomicNum()
        for atom in result.GetAtoms()
        if atom.GetAtomicNum() == 1
        for neighbor in atom.GetNeighbors()
    }
    assert explicit_h_parents == {8}


def test_structure_source_selects_original_or_current(tmp_path):
    original = tmp_path / "original.sdf"
    current = tmp_path / "current.sdf"
    _write_mol(original, "CC")
    _write_mol(current, "CCC")
    row = {"id": 1, "stored_path": str(original), "current_path": str(current)}

    assert ligand_working_path(row, structure_source="original") == original
    assert ligand_working_path(row, structure_source="current") == current


def test_protonation_promotes_new_artifact_without_overwriting_input(tmp_path):
    project_root = tmp_path / "project"
    output_dir = project_root / "data" / "molecules"
    source = output_dir / "original" / "ethanol.sdf"
    _write_mol(source, "CCO")
    before = source.read_text(encoding="utf-8")

    result = transform_ligand_rows(
        operation="protonate",
        output_dir=output_dir,
        rows=[{
            "id": 3,
            "stored_path": str(source),
            "current_path": str(source),
            "current_model_index": None,
            "extra_data": {},
            "has_3d": False,
        }],
        params={"method": "polar_hydrogens", "structure_source": "current", "run_id": "abc123"},
    )

    update = result["updates"][0]
    promoted = project_root / update["current_path"]
    assert source.read_text(encoding="utf-8") == before
    assert promoted != source
    assert promoted.is_file()
    assert update["state"]["has_hs"] is True


def test_pkasso_gpu_is_requested_once_per_database_batch(tmp_path, monkeypatch):
    def _rows(_project_db, **_kwargs):
        yield from (
            {"id": index, "stored_path": f"{index}.sdf", "current_path": f"{index}.sdf"}
            for index in range(1, 6)
        )

    # The feed reads through `iter_ligand_rows` (scope_spec + db_pages); what this test checks is
    # the chunking and the per-chunk GPU flag, so the source is swapped out, not the database.
    monkeypatch.setattr("amdockvs.chemistry.jobs.iter_ligand_rows", _rows)
    monkeypatch.setattr("amdockvs.chemistry.jobs.max_model_index_by_molecule_ids", lambda _db, _ids: {})
    params = LigandChemistryJobParams(
        operation="protonate",
        batch_size=2,
        ligand_filters={"molecule_type": "small_molecule"},
        params={"method": "pkasso", "gpu": True},
    )

    batches = list(_iter_ligand_chemistry_batches(
        project_db=object(),
        db_path=tmp_path / "project.db",
        output_dir=tmp_path / "data" / "molecules",
        params=params,
    ))

    assert [len(batch["rows"]) for batch in batches] == [2, 2, 1]
    assert [batch["_gpu_required"] for batch in batches] == [1, 1, 1]


def test_pdb2pqr_hands_downstream_a_pdb_not_a_pqr(tmp_path, monkeypatch):
    """A PQR has no element column, so Meeko's receptor prep dies on it ("Element '' not
    found"). pdb2pqr must be asked for the PDB too, and that is what we return."""
    import subprocess

    from amdockvs.chemistry.tools import receptors

    seen: dict[str, object] = {}
    monkeypatch.setattr(receptors.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        receptors.subprocess,
        "run",
        lambda cmd, **kwargs: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    output = receptors.protonate_receptor_pdb2pqr_file(
        source_path=tmp_path / "receptor.pdb", output_path=tmp_path / "protonated_0.pdb"
    )

    assert output.suffix == ".pdb"
    assert f"--pdb-output={output}" in seen["cmd"]
    assert seen["cmd"][-1].endswith("protonated_0.pqr")  # the PQR is the sidecar, not the result
