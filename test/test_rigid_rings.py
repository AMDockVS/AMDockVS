import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.docking.engines.qvina import qvina_command, qvina_dock_runner
from amdockvs.docking.engines.vina import _count_heavy_atoms_pdbqt
from amdockvs.docking.preparation.meeko import dockable_ligand, rigid_ring_ligand

DATA = Path(__file__).resolve().parents[1] / "data" / "docking"
BOX = {"box_center": [15.190, 53.903, 16.917], "box_size": [20.0, 20.0, 20.0]}


def _types(path: Path) -> set[str]:
    return {line.split()[-1] for line in path.read_text().splitlines() if line.startswith(("ATOM", "HETATM"))}


def _azepane(tmp_path: Path) -> Path:
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles("CC(=O)N1CCCCCC1c1ccccc1O"))  # 7-ring: Meeko opens it
    AllChem.EmbedMolecule(mol, randomSeed=1)
    path = tmp_path / "azepane.pdbqt"
    path.write_text(PDBQTWriterLegacy.write_string(MoleculePreparation().prepare(mol)[0])[0])
    return path


def test_open_rings_become_rigid_with_the_same_atoms(tmp_path):
    ligand = _azepane(tmp_path)
    assert {"CG0", "G0"} <= _types(ligand)
    rigid = rigid_ring_ligand(ligand, tmp_path / "rigid")
    assert rigid != ligand and not any(t.startswith(("G", "CG")) for t in _types(rigid))
    assert _count_heavy_atoms_pdbqt(ligand) == _count_heavy_atoms_pdbqt(rigid) == 17
    assert rigid_ring_ligand(rigid, tmp_path / "again") == rigid  # nothing to do: same file


def test_only_vina_scoring_docks_open_rings(tmp_path):
    ligand = _azepane(tmp_path)
    dock = lambda program, scoring: dockable_ligand(ligand, program=program, scoring_function=scoring,
                                                    output_dir=tmp_path)
    assert dock("vina", "vina") == dock("vina", "vinardo") == ligand
    assert ligand not in {dock("vina", "ad4"), dock("qvina", "vina"), dock("gnina", None), dock("adgpu", None)}


def _installed() -> bool:
    try:
        return bool(qvina_command())
    except RuntimeError:
        return False


@pytest.mark.skipif(not _installed(), reason="QuickVina 2 binary not installed")
def test_qvina_docks_a_ligand_with_an_open_ring(tmp_path):
    rows = qvina_dock_runner(
        {
            "pairs": [
                {
                    "ligand_id": 1,
                    "receptor_id": 2,
                    "ligand_path": str(_azepane(tmp_path)),
                    "receptor_path": str(DATA / "1iep_receptor.pdbqt"),
                    "exhaustiveness": 2,
                    "num_modes": 1,
                    **BOX,
                }
            ],
            "output_dir": str(tmp_path / "out"),
            "vina_cpu": 2,
            "seed": 1,
        }
    )
    assert rows[0]["score"] is not None and rows[0]["score"] < 0, rows[0]["metrics"].get("error")
