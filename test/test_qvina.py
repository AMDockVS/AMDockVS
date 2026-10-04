import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.engines import vina
from amdockvs.docking.engines.programs import get_docking_program
from amdockvs.docking.engines.qvina import qvina_command, qvina_dock_runner
from amdockvs.docking.protocols import protocol_hash

DATA = Path(__file__).resolve().parents[1] / "data" / "docking"
BOX = {"box_center": [15.190, 53.903, 16.917], "box_size": [20.0, 20.0, 20.0]}


def _installed() -> bool:
    try:
        return bool(qvina_command())
    except RuntimeError:
        return False


def test_program_is_independent_of_vina():
    program = get_docking_program("qvina")
    assert (program.docking_engine, program.preparation_engine) == ("qvina", "ad4")
    with pytest.raises(ValueError):  # one scoring function: nothing to choose
        program.validate_config({"scoring_function": "vinardo"})
    config = {"exhaustiveness": 8}
    assert protocol_hash(program="qvina", config=config) != protocol_hash(program="vina", config=config)


def test_command_line_has_only_the_options_quickvina_knows(monkeypatch, tmp_path):
    seen: list[str] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **_kwargs):
            seen.extend(command)
            Path(command[command.index("--out") + 1]).write_text("REMARK VINA RESULT:      -8.5      0.000      0.000\n")

        def communicate(self):
            return "", ""

    monkeypatch.setattr(vina.subprocess, "Popen", FakeProcess)
    energies, _ = vina._run_vina_binary(
        vina_command=sys.executable,
        receptor_path=tmp_path / "r.pdbqt",
        ligand_path=tmp_path / "l.pdbqt",
        output_path=tmp_path / "out.pdbqt",
        spacing=0.375,
        scoring_function="vina",
        cpu=1,
        seed=1,
        exhaustiveness=8,
        num_modes=9,
        min_rmsd=1.0,
        energy_range=3.0,
        quickvina=True,
        **BOX,
    )
    assert energies == [[-8.5, 0.0, 0.0]]
    assert not {"--scoring", "--spacing", "--min_rmsd", "--verbosity"} & set(seen)
    assert {"--receptor", "--ligand", "--center_x", "--size_z", "--exhaustiveness", "--energy_range"} <= set(seen)


@pytest.mark.skipif(not _installed(), reason="QuickVina 2 binary not installed")
def test_qvina_docks_1iep(tmp_path):
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    from rdkit import Chem

    mol = Chem.AddHs(Chem.MolFromMolFile(str(DATA / "1iep_ligand.sdf")), addCoords=True)
    text, ok, error = PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(mol)[0])
    assert ok, error
    ligand = tmp_path / "ligand.pdbqt"
    ligand.write_text(text)

    rows = qvina_dock_runner(
        {
            "pairs": [
                {
                    "ligand_id": 1,
                    "receptor_id": 2,
                    "ligand_path": str(ligand),
                    "receptor_path": str(DATA / "1iep_receptor.pdbqt"),
                    "exhaustiveness": 4,
                    "num_modes": 3,
                    **BOX,
                }
            ],
            "output_dir": str(tmp_path / "out"),
            "vina_cpu": 2,
            "seed": 42,
        }
    )
    assert 1 <= len(rows) <= 3
    best = rows[0]
    assert (best["engine"], best["score_type"]) == ("qvina", "vina_score")
    assert best["score"] < -8.0, best["metrics"].get("error")  # imatinib in ABL: about -11 kcal/mol
    assert (tmp_path / "out" / "1__2.dock.sdf").stat().st_size > 0  # Meeko read the poses back

