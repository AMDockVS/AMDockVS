import ast
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


def test_pkasso_is_handed_a_bridge_script_that_exists(monkeypatch):
    """The package reorganization broke the bridge twice. The call still named
    `chemistry/pkasso_runner.py` after the file had moved, so pKasso's Python exited with 2 for
    every molecule. And it had moved to `integrations/pkasso.py`: run as a script, its folder
    comes first on the path, so `from pkasso import ...` imported the bridge itself. The tests
    never noticed either, because they never looked at the script pKasso was given."""
    from amdockvs.chemistry import protonation

    commands: list[list[str]] = []

    def bridge(command: list[str], **_kwargs) -> None:
        """Stands in for pKasso: records what it was handed and answers as the bridge does."""
        commands.append(command)
        molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
        molecule.SetProp("_AMDockID", "1")
        writer = Chem.SDWriter(command[command.index("--output") + 1])
        writer.write(molecule)
        writer.close()

    monkeypatch.setattr(protonation, "_run", bridge)

    result = protonation._pkasso([(1, Chem.MolFromSmiles("CCO"))], python=Path("python"), ph=7.4,
                                 model="molgpka", threads=1, gpu=False)

    script = Path(commands[0][1])
    assert script.is_file(), f"pKasso would be asked to run {script}, which does not exist"
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8")))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert script.stem not in imported, f"{script.name} would import itself instead of {script.stem}"
    assert list(result) == [1]  # and what it wrote comes back keyed by the molecule


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


# What reduce 4.15 really writes. The fakes used to answer "TRIMMED" and "PROTONATED" with exit 0
# for -BUILD; real reduce writes atom records, and its exit status does not track its result.
_HEADER = "USER  MOD reduce.4.15.250408 H: found=0, std=0, add=0, rem=0\n"
_TRIMMED = (
    "ATOM      1  N   ALA A   1      11.104   6.134  -6.504  1.00  0.00           N\n"
    "ATOM      2  CA  ALA A   1      11.639   6.071  -5.147  1.00  0.00           C\n"
    "END\n"
)
_BUILT = _TRIMMED.replace("END\n", (
    "ATOM      3  H   ALA A   1      10.140   6.329  -6.676  1.00  0.00           H   new\n"
    "ATOM      4  HA  ALA A   1      12.731   6.098  -5.172  1.00  0.00           H   new\n"
    "END\n"
))
_CHAIN_BREAK = "*WARNING*: Residues PRO 417  and ILE 435  in chain  A appear unbonded\n"


def _fake_reduce(monkeypatch, *, trim: tuple[int, str, str], build: tuple[int, str, str]) -> list:
    """Replace reduce for this thread: `trim` and `build` are its (exit, stdout, stderr)."""
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
        code, out, err = trim if "-Trim" in argv else build
        result = subprocess.CompletedProcess(argv, code, out, err)
        if kwargs.get("check"):  # as the real one does: a fake that ignores `check` hides it
            result.check_returncode()
        return result

    monkeypatch.setattr(receptors.subprocess, "run", run)
    return calls


def test_reduce_accepts_trim_exit_255_and_uses_the_managed_executable(tmp_path, monkeypatch):
    from amdockvs.chemistry.tools import receptors

    calls = _fake_reduce(monkeypatch, trim=(255, _TRIMMED, ""), build=(0, _BUILT, ""))
    result = receptors.protonate_receptor_reduce_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb",
                                                     command="/managed/bin/reduce")
    assert result.read_text() == _BUILT
    assert [argv[0] for argv, _kw in calls] == ["/managed/bin/reduce"] * 2  # -Trim, then -BUILD
    assert calls[1][1]["input"] == _TRIMMED


def test_reduce_keeps_a_complete_result_it_exited_1_on(tmp_path, monkeypatch):
    """reduce -BUILD exits 1 for a chain break -- a warning, with every hydrogen written. Taking the
    exit status for the result threw away a correct protonation of nearly every crystal structure
    with a missing loop (found on the tutorial's own Vps34 receptor)."""
    from amdockvs.chemistry.tools import receptors

    _fake_reduce(monkeypatch, trim=(255, _TRIMMED, ""), build=(1, _BUILT, _CHAIN_BREAK))
    result = receptors.protonate_receptor_reduce_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb",
                                                     command="/managed/bin/reduce")
    assert result.read_text() == _BUILT


@pytest.mark.parametrize(("trim", "build", "message"), [
    # An input reduce cannot read: exit 0, and nothing but its header back.
    ((0, _HEADER, ""), (0, _HEADER, ""), "found no atoms to protonate"),
    # A -BUILD that lost atoms on the way is no protonation, whatever it exits with.
    ((255, _TRIMMED, ""), (0, _HEADER + "END\n", ""), "returned 0 atoms for the 2"),
])
def test_reduce_refuses_a_result_with_atoms_missing_whatever_it_exits_with(tmp_path, monkeypatch, trim, build, message):
    """The other way the exit status lied: 0 with no atoms, which saved an empty structure as the
    protonated receptor and reported success."""
    from amdockvs.chemistry.tools import receptors

    _fake_reduce(monkeypatch, trim=trim, build=build)
    with pytest.raises(RuntimeError, match=message):
        receptors.protonate_receptor_reduce_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb",
                                                 command="/managed/bin/reduce")
    assert not (tmp_path / "out.pdb").exists()


# The GPU probe's answer when CUDA is available but runs nothing: a card outside the architectures
# the PyTorch build has kernels for, on either side of them. Each fails every molecule the same way.
_NO_KERNEL = "CUDA error: no kernel image is available for execution on the device"
_CARD_OLDER_THAN_THE_BUILD = {
    "usable": False, "name": "Example GPU", "capability": "6.1",
    "archs": ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"], "error": _NO_KERNEL,
}
_CARD_NEWER_THAN_THE_BUILD = {
    "usable": False, "name": "Example GPU", "capability": "13.0",
    "archs": ["sm_50", "sm_60", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"], "error": _NO_KERNEL,
}


@pytest.mark.parametrize(("usable", "on_gpu"), [(False, False), (True, True)])
def test_the_pkasso_bridge_runs_uni_pka_on_the_cpu_when_the_gpu_cannot_be_used(
        tmp_path, monkeypatch, capsys, usable, on_gpu):
    """It decides where the job runs, so a worker whose card differs from the submitter's is right too."""
    import json
    import sys
    from types import SimpleNamespace

    from amdockvs.integrations import pkasso_runner

    asked = {}

    def batch_protonate(smiles, *, model, **_kwargs):
        asked["model"] = model
        return smiles, [[Chem.MolFromSmiles(each)] for each in smiles]

    monkeypatch.setitem(sys.modules, "pkasso", SimpleNamespace(batch_protonate=batch_protonate))
    monkeypatch.setattr(pkasso_runner, "gpu_support", lambda: {**_CARD_OLDER_THAN_THE_BUILD, "usable": usable})
    source, output = tmp_path / "in.json", tmp_path / "out.sdf"
    source.write_text(json.dumps([{"id": 1, "smiles": "CCO"}]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["pkasso_runner.py", "--input", str(source), "--output", str(output),
                                      "--ph", "7.4", "--model", "mixed", "--gpu"])

    assert pkasso_runner.main() == 0

    assert asked["model"]["unipka"]["gpu"] is on_gpu
    said = capsys.readouterr().err
    assert said.strip() == ("" if usable else
                            f"pKasso: the GPU cannot be used (Example GPU: {_NO_KERNEL}); running Uni-pKa on the CPU.")
    assert output.exists()


@pytest.mark.parametrize(("answer", "usable", "said"), [
    (_CARD_OLDER_THAN_THE_BUILD, False, "GPU: Example GPU (compute 6.1) can't be used by pKasso's PyTorch, "
                                        "built for compute 7.5 to 12.0; Uni-pKa runs on the CPU."),
    (_CARD_NEWER_THAN_THE_BUILD, False, "GPU: Example GPU (compute 13.0) can't be used by pKasso's PyTorch, "
                                        "built for compute 5.0 to 9.0; Uni-pKa runs on the CPU."),
    ({"usable": True, "name": "Example GPU", "capability": "8.9", "archs": ["sm_75", "sm_89"]},
     True, "GPU: Example GPU is ready."),
    # No CUDA GPU, or a driver too old for the build: PyTorch then reports CUDA unavailable.
    ({"usable": False, "archs": []}, False, "No GPU is visible to pKasso; Uni-pKa runs on the CPU."),
    (None, False, "GPU check failed (IndexError); Uni-pKa runs on the CPU."),  # it answered nothing
], ids=["card-older-than-the-build", "card-newer-than-the-build", "usable", "no-gpu", "no-answer"])
def test_the_pkasso_gpu_probe_says_what_the_card_can_do(tmp_path, monkeypatch, answer, usable, said):
    import json
    import subprocess
    from types import SimpleNamespace

    from amdockvs.chemistry import protonation

    printed = "" if answer is None else json.dumps(answer) + "\n"
    # Only this module's reference: a global subprocess.run fake would serve other threads too.
    monkeypatch.setattr(protonation, "subprocess", SimpleNamespace(
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, printed, "")))

    support = protonation.pkasso_gpu_support(tmp_path / "pkasso" / "bin" / "python")

    assert (support.usable, support.message) == (usable, said)


@pytest.mark.parametrize("usable", [False, True])
def test_a_protonation_asks_for_the_gpu_only_when_pkasso_can_use_it(monkeypatch, usable):
    """Asked for anyway, the job would hold a GPU it cannot run on and then fail every molecule."""
    from types import SimpleNamespace

    from amdockvs.chemistry import api as api_module
    from amdockvs.chemistry.protonation import GpuSupport

    monkeypatch.setattr(api_module, "protonation_tool_status", lambda _runtime, _name: SimpleNamespace(
        installed=True, command=Path("/managed/pkasso/bin/python"), message="pKasso 0.6.1 is ready."))
    monkeypatch.setattr(api_module, "pkasso_gpu_support", lambda _python: GpuSupport(usable, ""))

    params = api_module.ChemistryAPI(runtime=None)._protonate_params(method="pkasso", model="mixed", gpu=True)

    assert params["gpu"] is usable


def test_pdb2pqr_says_why_it_gave_up(tmp_path, monkeypatch):
    """pdb2pqr refuses a structure with a gap in its backbone -- rightly -- and says so in its
    CRITICAL lines, then prints a traceback ending in a bare RuntimeError. It ran with
    `check=True`, whose error keeps none of that: "returned non-zero exit status 1" was all a
    user learned about why their receptor could not be protonated."""
    import subprocess

    from amdockvs.chemistry.tools import receptors

    said = (  # what pdb2pqr 3.7.1 writes for 1iep's SER A 438, trimmed
        "INFO:Checking and transforming input biomolecule...\n"
        "WARNING:Missing atom OXT in residue GLN A 498\n"
        "CRITICAL:Too few atoms present to reconstruct or cap residue SER A 438 in structure! This error "
        "is generally caused by missing backbone atoms in this biomolecule; you must use an external "
        "program to complete gaps in the biomolecule backbone.\n"
        "CRITICAL:Giving up.\n"
        "Traceback (most recent call last):\n" + "  File \"main.py\", line 1, in <module>\n" * 40
        + "RuntimeError\n"
    )

    def run(argv, **kwargs):
        result = subprocess.CompletedProcess(argv, 1, "", said)
        if kwargs.get("check"):
            result.check_returncode()
        return result

    monkeypatch.setattr("ms_env.locate", lambda recipe: ("ready", recipe.candidates[0]))
    monkeypatch.setattr(receptors.subprocess, "run", run)
    with pytest.raises(RuntimeError) as raised:
        receptors.protonate_receptor_pdb2pqr_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb",
                                                  command="/managed/bin/pdb2pqr")
    message = str(raised.value)
    assert "SER A 438" in message and "complete gaps in the biomolecule backbone" in message
    assert message.startswith("pdb2pqr exited with 1")


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
