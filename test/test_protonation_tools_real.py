"""Every protonation tool, run for real: the ones the unit tests can only fake.

Each fault found here so far was hidden by a fake that assumed how a tool behaves. pKasso was
handed a script that had moved away, and the fake subprocess never looked at what it was handed.
reduce exits 1 on a complete result and 0 with nothing written, and its fake exited 0 with
"PROTONATED". The tests here run the installed tools on real molecules, through the functions the
jobs call, and check the chemistry that comes back.

The first test needs no tool and always runs: it checks the script each call site hands its tool's
own interpreter. A tool that is not installed is skipped, with the reason; run with `-rs` to see
what was skipped.
With an isolated HOME, set ``AMDOCK_TOOLS_HOME`` to a tools home that has them, e.g.
``AMDOCK_TOOLS_HOME=~/.amdockvs/tools``.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

Chem = pytest.importorskip("rdkit.Chem")

ROOT = Path(__file__).resolve().parents[1]
# The tutorial's receptor: complete residues, but two chain breaks, on which reduce exits 1.
RECEPTOR = ROOT / "src" / "amdockvs" / "data" / "tutorials" / "simple_docking" / "Vps34_receptor.pdb"

LIGANDS = {1: "CC(=O)O", 2: "CCN", 3: "c1ccccc1O", 4: "OC(=O)CC(N)C(=O)O"}
NAMES = {1: "acetic acid", 2: "ethylamine", 3: "phenol", 4: "aspartic acid"}
# At pH 7.4 the acids are deprotonated, the primary amine protonated, phenol (pKa ~10) neutral,
# and aspartic acid a zwitterion: two carboxylates and an ammonium.
CHARGE_AT_7_4 = {1: -1, 2: +1, 3: 0, 4: -1}


def _command(tool: str) -> str:
    from amdockvs.integrations.envs import protonation_tool_status

    status = protonation_tool_status(None, tool)
    if not status.installed:
        pytest.skip(f"{tool} is not installed here: {status.message}")
    return str(status.command)


def _pdb_counts(text: str) -> tuple[int, int, int]:
    """(atoms, hydrogens, atoms without an element column) of PDB text."""
    atoms = hydrogens = no_element = 0
    for line in text.splitlines():
        if line.startswith(("ATOM", "HETATM")):
            atoms += 1
            element = line[76:78].strip()
            no_element += not element
            hydrogens += (element or line[12:16].strip()[:1]) == "H"
    return atoms, hydrogens, no_element


def _stored_receptor() -> bytes:
    """The receptor as the project stores it: mmCIF, which the receptor tools convert to PDB."""
    gemmi = pytest.importorskip("gemmi")
    structure = gemmi.read_structure(str(RECEPTOR))
    structure.setup_entities()
    return structure.make_mmcif_document().as_string().encode()


def _pkasso_script(monkeypatch, tmp_path) -> Path:
    from amdockvs.chemistry import protonation

    handed: list[list[str]] = []

    def run(command, **_kwargs):  # answers as the bridge does, so the call completes
        handed.append(command)
        molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
        molecule.SetProp("_AMDockID", "1")
        writer = Chem.SDWriter(command[command.index("--output") + 1])
        writer.write(molecule)
        writer.close()

    monkeypatch.setattr(protonation, "_run", run)
    protonation._pkasso([(1, Chem.MolFromSmiles("CCO"))], python=Path("python"), ph=7.4,
                        model="molgpka", threads=1, gpu=False)
    return Path(handed[0][1])


def _openmm_script(monkeypatch, tmp_path) -> Path:
    from amdockvs.chemistry.tools import receptors

    handed: list[list[str]] = []
    monkeypatch.setattr(receptors, "_require_executable", lambda *_args: "python")
    monkeypatch.setattr(receptors.subprocess, "run",
                        lambda argv, **_kwargs: handed.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    receptors.fix_receptor_pdb_file(source_path=tmp_path / "in.pdb", output_path=tmp_path / "out.pdb")
    return Path(handed[0][1])


@pytest.mark.parametrize("handed_by", [_pkasso_script, _openmm_script], ids=["pkasso", "openmm"])
def test_a_tool_bridge_script_exists_and_cannot_import_itself(handed_by, monkeypatch, tmp_path):
    """A tool that lives in its own environment is handed a script of ours to run there. That
    script broke twice in one move: its caller kept the old path, and its new name shadowed the
    library -- Python puts a script's own folder first on its path. Needs no tool installed."""
    script = handed_by(monkeypatch, tmp_path)
    assert script.is_file(), f"the tool would be asked to run {script}, which does not exist"
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8")))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert script.stem not in imported, f"{script.name} would import itself instead of {script.stem}"


@pytest.mark.parametrize("method", ["dimorphite", "explicit_hs", "openbabel", "pkasso"])
def test_a_ligand_protonation_method_runs_for_real(method):
    from amdockvs.chemistry.protonation import protonate_molecule_batch

    params = {"ph": 7.4, "model": "molgpka", "threads": 1}
    if method in {"openbabel", "pkasso"}:
        params["tool_command"] = _command(method)

    result = protonate_molecule_batch(
        [(index, Chem.MolFromSmiles(smiles)) for index, smiles in LIGANDS.items()],
        method=method, params=params,
    )

    assert sorted(result) == sorted(LIGANDS), f"{method} lost molecules: {sorted(result)}"
    for index, molecule in result.items():
        name = NAMES[index]
        assert any(atom.GetAtomicNum() == 1 for atom in molecule.GetAtoms()), f"{method}: {name} has no hydrogens"
        # explicit_hs has no pH model by design: it keeps the (neutral) charges it was given.
        expected = 0 if method == "explicit_hs" else CHARGE_AT_7_4[index]
        charge = Chem.GetFormalCharge(molecule)
        assert charge == expected, (
            f"{method}: {name} came back as {Chem.MolToSmiles(Chem.RemoveHs(molecule))} "
            f"(charge {charge:+d}), expected {expected:+d} at pH 7.4"
        )


@pytest.mark.parametrize("method", ["reduce", "pdb2pqr"])
def test_a_receptor_protonation_method_runs_for_real_on_a_stored_receptor(method):
    """Through the function a receptor job calls, from the stored mmCIF, on a receptor with chain
    breaks -- which reduce answers with exit 1 and a complete result."""
    from amdockvs.chemistry.service import transform_receptor_rows

    command = _command(method)
    stored = _stored_receptor()
    heavy = _pdb_counts(RECEPTOR.read_text())[0]

    [update] = transform_receptor_rows(
        operation="protonate",
        rows=[{"id": 1, "structure": stored, "structure_kind": "cif"}],
        params={"method": method, "tool_command": command, "ph": 7.0},
    )

    atoms, hydrogens, no_element = _pdb_counts(update["current_payload_id"].data.decode())
    assert hydrogens > heavy // 2, f"{method} added {hydrogens} hydrogens to {heavy} heavy atoms"
    assert atoms >= heavy + hydrogens - 10, f"{method} returned {atoms} atoms for {heavy} heavy ones"
    assert no_element == 0, f"{method} wrote {no_element} atoms with no element column"
    assert update["state"]["has_hs"] is True and update["state"]["protonation_model"] == method


def test_the_openmm_protein_tools_run_for_real_as_the_app_chains_them():
    """PDBFixer and OpenMM run as a script in their own environment, the same way the pKasso
    bridge does -- and the same way that broke. Fix, then minimize, as Build chains them: the raw
    receptor lacks its C-terminal oxygen, which OpenMM's force field rightly refuses and Fix adds."""
    from amdockvs.chemistry.service import transform_receptor_rows

    _command("openmm")
    heavy = _pdb_counts(RECEPTOR.read_text())[0]

    def run(operation: str, data: bytes, kind: str, **params) -> str:
        [update] = transform_receptor_rows(
            operation=operation, rows=[{"id": 1, "structure": data, "structure_kind": kind}], params=params,
        )
        return update["current_payload_id"].data.decode()

    fixed = run("fix", _stored_receptor(), "cif")
    atoms, _hydrogens, no_element = _pdb_counts(fixed)
    assert atoms >= heavy, f"PDBFixer returned {atoms} atoms for {heavy} heavy ones"
    assert no_element == 0

    # Minimize rebuilds every hydrogen to fit the force field, so it needs no protonation first.
    minimized = run("minimize", fixed.encode(), "pdb", max_iterations=20, tolerance_kj_mol=10.0)
    atoms, hydrogens, no_element = _pdb_counts(minimized)
    assert hydrogens > heavy // 2, f"OpenMM rebuilt {hydrogens} hydrogens for {heavy} heavy atoms"
    assert atoms >= heavy + hydrogens - 10 and no_element == 0
