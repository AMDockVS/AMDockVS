from pathlib import Path

from rdkit import Chem

from amdockvs.chemistry.protonation import protonate_molecule_batch
from amdockvs.chemistry.jobs import LigandChemistryJobParams, _iter_ligand_chemistry_batches
from amdockvs.chemistry.service import transform_ligand_rows


def _molblock(smiles: str) -> bytes:
    return Chem.MolToMolBlock(Chem.MolFromSmiles(smiles)).encode()


def test_explicit_hs_adds_every_hydrogen_and_keeps_charges():
    source = Chem.MolFromSmiles("[NH3+]CCC(=O)[O-]")

    result = protonate_molecule_batch([(7, source)], method="explicit_hs", params={})[7]

    assert sum(atom.GetAtomicNum() == 1 for atom in result.GetAtoms()) == 7
    assert Chem.MolToSmiles(Chem.RemoveHs(result)) == Chem.MolToSmiles(source)


def _seeded(tmp_path, original: str, current: str):
    """A project with one ligand whose imported structure is `original` and current one `current`."""
    from ms_flow.core.database.project import ProjectStore

    from amdockvs.models.molecules import MoleculeRecord
    from payload_seed import seed_payload

    store = ProjectStore.open_at(tmp_path / "project.db")
    stored = seed_payload(store, _molblock(original), "sdf")
    now = seed_payload(store, _molblock(current), "sdf")
    with store.get_session() as session:
        session.add(MoleculeRecord(name="lig", is_ligand=True, molecule_type="small_molecule",
                                   stored_payload_id=stored, current_payload_id=now))
        session.commit()
    return store


def test_structure_source_selects_original_or_current(tmp_path):
    from amdockvs.chemistry.structures import mol_from_bytes, with_structures

    store = _seeded(tmp_path, "CC", "CCC")
    rows = [{"id": 1, "stored_payload_id": 1, "current_payload_id": 2}]

    original, = with_structures(store, rows, original=True)
    current, = with_structures(store, rows)

    assert mol_from_bytes(original["structure"], original["structure_kind"]).GetNumAtoms() == 2
    assert mol_from_bytes(current["structure"], current["structure_kind"]).GetNumAtoms() == 3
    store.dispose()


def test_protonation_returns_a_new_payload_and_leaves_the_input_alone():
    from ms_flow.payloads import Payload

    source = _molblock("CCO")
    result = transform_ligand_rows(
        operations="protonate",
        rows=[{
            "id": 3,
            "structure": source,
            "structure_kind": "sdf",
            "extra_data": {},
            "has_3d": False,
        }],
        params={"method": "explicit_hs"},
    )

    update = result["updates"][0]
    promoted = update["current_payload_id"]
    assert isinstance(promoted, Payload) and promoted.data != source
    assert source == _molblock("CCO")
    assert update["state"]["has_hs"] is True
    assert update["model_rows"] == []  # protonating makes no model: the current 3D one stays


def test_pkasso_gpu_is_requested_once_per_database_batch(tmp_path, monkeypatch):
    from payload_seed import seed_payload

    from ms_flow.core.database.project import ProjectStore

    store = ProjectStore.open_at(tmp_path / "project.db")
    structure = seed_payload(store, _molblock("CCO"), "sdf")

    def _rows(_project_db, **_kwargs):
        yield from ({"id": index, "current_payload_id": structure} for index in range(1, 6))

    # The feed reads through `iter_ligand_rows` (scope_spec + db_pages); what this test checks is
    # the chunking and the per-chunk GPU flag, so the source is swapped out, not the database.
    monkeypatch.setattr("amdockvs.chemistry.jobs.iter_ligand_rows", _rows)
    params = LigandChemistryJobParams(
        operation="protonate",
        batch_size=2,
        ligand_filters={"molecule_type": "small_molecule"},
        params={"method": "pkasso", "gpu": True},
    )

    batches = list(_iter_ligand_chemistry_batches(project_db=store, params=params))

    assert [len(batch["rows"]) for batch in batches] == [2, 2, 1]
    assert [batch["_gpu_required"] for batch in batches] == [1, 1, 1]
    assert {row["structure"] for batch in batches for row in batch["rows"]} == {_molblock("CCO")}  # the bytes ride along
    store.dispose()


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


def test_external_tools_get_scratch_files_that_are_gone_afterwards(tmp_path):
    """OpenBabel stand-in: copies its input SDF to the output path; no file survives the call."""
    fake = tmp_path / "obabel"
    fake.write_text("#!/bin/sh\n# args: -isdf IN -osdf -O OUT -p PH\ncp \"$2\" \"$5\"\necho \"$2 $5\" > " + str(tmp_path / "seen") + "\n")
    fake.chmod(0o755)

    result = protonate_molecule_batch(
        [(5, Chem.MolFromSmiles("CCO"))], method="openbabel", params={"tool_command": str(fake)}
    )

    assert set(result) == {5} and result[5].GetNumAtoms() == 9  # ethanol with every H
    for name in (tmp_path / "seen").read_text().split():
        assert not Path(name).exists()
