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
    from ms_adtools import prepare_ligand
    from rdkit import Chem

    mol = Chem.AddHs(Chem.MolFromMolFile(str(DATA / "1iep_ligand.sdf")), addCoords=True)
    text = prepare_ligand(mol, open_macrocycles=True)
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


FAKE_UNIDOCK = """#!{python}
import sys, time
from pathlib import Path
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
ligands = Path(args["--ligand_index"]).read_text().split()
pose = Path("{pose}").read_text()
for k, batch in enumerate((ligands[:2], ligands[2:]), start=1):  # two GPU batches, like the real one
    deadline = time.time() + 10  # batch 2 waits until batch 1 is reported (its SDFs are written)
    while k == 2 and len(list(Path(args["--dir"]).parent.glob("*.sdf"))) < 2 and time.time() < deadline:
        time.sleep(0.05)
    print(f"Small Batch {{k}} size: {{len(batch)}}", flush=True)
    for ligand in batch:
        Path(args["--dir"], Path(ligand).stem + "_out.pdbqt").write_text(
            "MODEL 1\\nREMARK VINA RESULT:    -7.0      0.000      0.000\\n" + pose + "ENDMDL\\n")
    print(f"Batch {{k}} running time: 10ms", flush=True)
"""


def test_each_gpu_batch_is_reported_as_soon_as_it_ends(tmp_path, monkeypatch):
    data = Path(__file__).resolve().parent / "data"
    ligand = data / "1s63_ligand.pdbqt"
    fake = tmp_path / "unidock"
    fake.write_text(FAKE_UNIDOCK.format(python=sys.executable, pose=ligand))
    fake.chmod(0o755)
    monkeypatch.setattr(unidock, "unidock_command", lambda: str(fake))
    xyz = [[float(line[30 + 8 * i:38 + 8 * i]) for i in range(3)]
           for line in ligand.read_text().splitlines() if line.startswith(("ATOM", "HETATM"))]
    center = [sum(axis) / len(xyz) for axis in zip(*xyz)]
    receptor = tmp_path / "receptor.pdbqt"  # no zinc: the standard method refuses a box holding one
    receptor.write_text("".join(line for line in (data / "1s63_zn_site.pdbqt").open() if " Zn" not in line[66:]))
    out = tmp_path / "out"
    seen = []  # (ligand id, how many poses of the second batch existed when it was reported)

    def told(rows):
        written = len([p for p in out.glob("_unidock_*/*_out.pdbqt") if p.name.startswith(("3__", "4__"))])
        seen.append((rows[0]["ligand_molecule_id"], written, rows[0]["error"]))

    rows = unidock.unidock_dock_runner({
        "pairs": [{"ligand_id": i, "receptor_id": 2, "ligand_path": str(ligand),
                   "receptor_path": str(receptor),
                   "box_center": center, "box_size": [22.0] * 3} for i in (1, 2, 3, 4)],
        "output_dir": str(out),
        "_pair_callback": told,
        "_collect_rows": False,
    })
    assert rows == []
    assert [(lig, err) for lig, _, err in seen] == [(1, None), (2, None), (3, None), (4, None)], seen
    assert [written for lig, written, _ in seen if lig in (1, 2)] == [0, 0]  # before batch 2 existed
