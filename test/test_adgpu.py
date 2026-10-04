import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.engines import adgpu, autogrid
from amdockvs.docking.engines.programs import chunk_resources, get_docking_program, list_docking_programs
from amdockvs.docking.protocols import protocol_hash

DATA = Path(__file__).resolve().parents[1] / "data" / "docking"
BOX = {"box_center": [15.190, 53.903, 16.917], "box_size": [20.0, 20.0, 20.0]}
ATOM = "ATOM      1  X   LIG A   1       0.000   0.000   0.000  1.00  0.00     0.000 {}\n"


def _installed() -> bool:
    try:
        return bool(adgpu.adgpu_command()) and bool(autogrid.shutil.which(autogrid.AUTOGRID4))
    except RuntimeError:
        return False


def test_program_replaces_autodock4_and_takes_the_gpu():
    assert "autodock4" not in {program.key for program in list_docking_programs()}
    program = get_docking_program("adgpu")
    assert (program.docking_engine, program.preparation_engine) == ("adgpu", "ad4")
    assert chunk_resources("adgpu", {"nrun": 20, "scoring_function": "vina"}) == {"_gpu_required": 1}
    # A different force field is a different protocol: its results must not be skipped as existing.
    assert protocol_hash(program="adgpu", config={"nrun": 20, "parameter_file": "/a.dat"}) != protocol_hash(
        program="adgpu", config={"nrun": 20, "parameter_file": ""}
    )


def test_maps_cover_flexible_types_and_follow_the_parameter_file(monkeypatch, tmp_path):
    (tmp_path / "rec.pdbqt").write_text(ATOM.format("N "))
    (tmp_path / "ligand.pdbqt").write_text(ATOM.format("C ") + ATOM.format("OA"))
    (tmp_path / "rec__flex.pdbqt").write_text(ATOM.format("HD") + ATOM.format("C "))
    parameters = tmp_path / "ff.dat"
    parameters.write_text("atom_par C 4.00 0.150 33.5103 -0.00143 0.0 0.0 0 -1 -1 0\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(autogrid, "AUTOGRID4", sys.executable)
    monkeypatch.setattr(
        autogrid.subprocess, "run", lambda command, **_kw: commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
    )
    cache: dict = {}

    def prefix(parameter_file):
        return autogrid.map_prefix(
            cache=cache,
            maps_dir=tmp_path / "maps",
            receptor_path=tmp_path / "rec.pdbqt",
            ligand_path=tmp_path / "ligand.pdbqt",
            flex_receptor_path=tmp_path / "rec__flex.pdbqt",
            spacing=0.375,
            parameter_file=parameter_file,
            **BOX,
        )

    default, custom = prefix(None), prefix(parameters)
    assert default != custom and len(commands) == 2
    gpf = Path(f"{custom}.gpf").read_text().splitlines()
    assert gpf[0] == "parameter_file parameters.dat"
    assert "ligand_types C HD OA" in gpf  # without the HD map the flexible residue cannot be docked
    assert "parameter_file" not in Path(f"{default}.gpf").read_text()
    parameters.write_text("atom_par C 9.99 0.150 33.5103 -0.00143 0.0 0.0 0 -1 -1 0\n")
    assert prefix(parameters) not in {default, custom}  # edited in place: new maps


def test_a_failed_job_is_an_error_although_the_exit_code_is_zero(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(command, **_kw):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "Error: Ligand includes atom with unknown type: XX.\n", "")

    monkeypatch.setattr(adgpu.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="unknown type: XX"):
        adgpu._run_adgpu(
            executable="adgpu",
            maps_prefix=str(tmp_path / "rec"),
            ligand_path=tmp_path / "ligand.pdbqt",
            flex_receptor_path=None,
            output_dir=tmp_path,
            name="1__2.adgpu",
            nrun=20,
            seed=0,
            derived_types="XX=OA",
        )
    assert seen[0][seen[0].index("--derivtype") + 1] == "XX=OA" and "--seed" not in seen[0]


@pytest.mark.skipif(not _installed(), reason="AutoDock-GPU or autogrid4 not installed")
def test_adgpu_docks_1iep(tmp_path):
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    from rdkit import Chem

    mol = Chem.AddHs(Chem.MolFromMolFile(str(DATA / "1iep_ligand.sdf")), addCoords=True)
    text, ok, error = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(mol)[0])
    assert ok, error
    ligand = tmp_path / "ligand.pdbqt"
    ligand.write_text(text)

    rows = adgpu.adgpu_dock_runner(
        {
            "pairs": [
                {
                    "ligand_id": 1,
                    "receptor_id": 2,
                    "ligand_path": str(ligand),
                    "receptor_path": str(DATA / "1iep_receptor.pdbqt"),
                    **BOX,
                }
            ],
            "output_dir": str(tmp_path / "out"),
            "nrun": 10,
            "num_modes": 3,
            "seed": 42,
        }
    )
    assert 1 <= len(rows) <= 3
    best = rows[0]
    assert (best["engine"], best["score_type"]) == ("adgpu", "ad4_score")
    assert best["score"] < -8.0, best["metrics"].get("error")  # imatinib in ABL: about -10.7 kcal/mol
    assert [row["score"] for row in rows] == sorted(row["score"] for row in rows)
    poses = (tmp_path / "out" / "1__2.adgpu.sdf").read_text()
    assert poses.count("$$$$") == len(rows)
