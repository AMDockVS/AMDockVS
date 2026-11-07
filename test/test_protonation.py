from pathlib import Path

import pytest

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
    monkeypatch.setattr(receptors, "_require_executable", lambda name, command=None: f"/managed/bin/{name}")
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
    assert "--titration-state-method=propka" in seen["cmd"]
    assert seen["cmd"][-1].endswith("protonated_0.pqr")  # the PQR is the sidecar, not the result


@pytest.mark.parametrize("method", ["reduce", "pdb2pqr"])
def test_receptor_api_passes_the_managed_command_to_the_job(monkeypatch, method):
    from types import SimpleNamespace
    from amdockvs.chemistry.api import ChemistryAPI

    api = ChemistryAPI(runtime=SimpleNamespace())
    command = f"/managed/{method}/bin/{method}"
    monkeypatch.setattr(api, "receptor_protonation_tool_status", lambda name: SimpleNamespace(
        installed=True, command=command))
    submitted = {}
    monkeypatch.setattr(api, "_submit_receptor_operation", lambda operation, **kw: submitted.update(kw) or "job")
    assert api.protonate_receptors(method=method, ph=6.5) == "job"
    assert submitted["params"]["tool_command"] == command
    assert submitted["params"]["ph"] == 6.5


def test_reduce_accepts_trim_exit_255_and_uses_the_managed_executable(tmp_path, monkeypatch):
    import subprocess
    import threading

    from amdockvs.chemistry.tools import receptors

    calls = []
    monkeypatch.setattr("ms_env.locate", lambda recipe: ("ready", recipe.candidates[0]))
    # `receptors.subprocess` is the one subprocess module, so this replaces run() for the whole
    # process. A worker thread another test left behind would otherwise be handed this fake and
    # land in `calls`, which is what these assertions are about. Serve only our own thread.
    ours, real_run = threading.current_thread(), subprocess.run

    def run(argv, **kwargs):
        if threading.current_thread() is not ours:
            return real_run(argv, **kwargs)
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 255 if "-Trim" in argv else 0,
                                           "TRIMMED\n" if "-Trim" in argv else "PROTONATED\n", "")

    monkeypatch.setattr(receptors.subprocess, "run", run)
    result = receptors.protonate_receptor_reduce_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb",
                                                     command="/managed/bin/reduce")
    assert result.read_text() == "PROTONATED\n"
    assert [argv[0] for argv, _kw in calls] == ["/managed/bin/reduce"] * 2  # -Trim, then -BUILD
    assert calls[1][1]["input"] == "TRIMMED\n"


def test_a_worker_resolves_its_own_tool_when_the_submitter_path_is_missing(monkeypatch):
    from types import SimpleNamespace
    from amdockvs.chemistry.tools import receptors
    from amdockvs.integrations import envs

    monkeypatch.setattr("ms_env.locate", lambda recipe: ("missing", ""))
    monkeypatch.setattr(envs, "protonation_tool_status", lambda runtime, name: SimpleNamespace(
        installed=True, command="/worker/bin/reduce"))
    assert receptors._require_executable("reduce", "/submitter/bin/reduce") == "/worker/bin/reduce"


@pytest.mark.parametrize("command_kind", ["missing", "bare", "old_absolute"])
def test_worker_never_uses_a_system_reduce_when_the_managed_install_is_missing(tmp_path, monkeypatch, command_kind):
    import os
    from amdockvs.chemistry.tools import receptors

    if os.name == "nt":
        pytest.skip("Shell stand-in for Reduce")
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path / "managed"))
    binary = tmp_path / "amber" / "bin" / "reduce"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho reduce.3.3.160602\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ.get("PATH", ""))
    command = {"missing": None, "bare": "reduce", "old_absolute": str(binary)}[command_kind]
    with pytest.raises(RuntimeError, match=r"reduce 4\.15.*Settings"):
        receptors._require_executable("reduce", command)


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
