import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.engines import unidock
from amdockvs.docking.engines.programs import chunk_resources, get_docking_program
from amdockvs.docking.protocols import protocol_hash

DATA = Path(__file__).resolve().parents[1] / "data" / "docking"
BOX = {"box_center": [15.190, 53.903, 16.917], "box_size": [20.0, 20.0, 20.0]}


def _installed() -> bool:
    try:
        return bool(unidock.unidock_command())
    except RuntimeError:
        return False


def test_program_takes_the_gpu_and_its_search_mode_is_the_protocol():
    program = get_docking_program("unidock")
    assert (program.docking_engine, program.preparation_engine) == ("unidock", "ad4")
    assert chunk_resources("unidock", {"scoring_function": "vina"}) == {"_gpu_required": 1}
    assert protocol_hash(program="unidock", config={"search_mode": "fast"}) != protocol_hash(
        program="unidock", config={"search_mode": "detail"}
    )


@pytest.mark.skipif(not _installed(), reason="Uni-Dock not installed")
def test_unidock_docks_1iep_in_one_run_and_a_bad_ligand_fails_alone(tmp_path):
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    from rdkit import Chem

    mol = Chem.AddHs(Chem.MolFromMolFile(str(DATA / "1iep_ligand.sdf")), addCoords=True)
    text, ok, error = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(mol)[0])
    assert ok, error
    ligand = tmp_path / "ligand.pdbqt"
    ligand.write_text(text)
    broken = tmp_path / "broken.pdbqt"
    broken.write_text("not a ligand\n")  # Uni-Dock aborts the whole run on it

    def pair(ligand_id, path):
        return {"ligand_id": ligand_id, "receptor_id": 2, "ligand_path": str(path),
                "receptor_path": str(DATA / "1iep_receptor.pdbqt"), **BOX}

    rows = unidock.unidock_dock_runner(
        {
            "pairs": [pair(1, ligand), pair(3, broken), pair(4, ligand)],
            "output_dir": str(tmp_path / "out"),
            "num_modes": 3,
            "seed": 42,
        }
    )
    failed = [row for row in rows if row["ligand_molecule_id"] == 3]
    assert len(failed) == 1 and failed[0]["error"], failed
    for ligand_id in (1, 4):
        poses = [row for row in rows if row["ligand_molecule_id"] == ligand_id]
        assert 1 <= len(poses) <= 3 and not poses[0]["error"], poses
        assert poses[0]["score_type"] == "vina_score"
        assert poses[0]["score"] < -9.0  # imatinib in ABL: about -12 kcal/mol
        assert [row["score"] for row in poses] == sorted(row["score"] for row in poses)
    assert (tmp_path / "out" / "1__2.unidock.sdf").read_text().count("$$$$") == len(
        [row for row in rows if row["ligand_molecule_id"] == 1])
    assert not list((tmp_path / "out").glob("_unidock_*"))
