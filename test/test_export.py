import zipfile
from types import SimpleNamespace

from ms_flow.core.database.project import ProjectStore

from amdockvs.io.export import export_poses_sdf, export_structures
from payload_seed import seed_payload

SDF = "\n  rdkit\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n"


def test_poses_leave_as_one_sdf_with_their_scores(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    first, second = seed_payload(store, SDF, "sdf"), seed_payload(store, SDF, "sdf")
    pdbqt = seed_payload(store, "ATOM\n", "pdbqt")
    hits = [
        SimpleNamespace(pose_payload_id=first, ligand_name="aspirin", receptor_name="1abc", engine="vina",
                        protocol_label="", score=-7.5, rmsd_vs_reference=None, ligand_efficiency=0.4,
                        predicted_pki=None, fit_quality=None),
        SimpleNamespace(pose_payload_id=second, ligand_name="caffeine", receptor_name="1abc", engine="vina",
                        protocol_label="", score=-6.1, rmsd_vs_reference=None, ligand_efficiency=None,
                        predicted_pki=None, fit_quality=None),
        SimpleNamespace(pose_payload_id=pdbqt, ligand_name="not an sdf", score=-1.0),
        SimpleNamespace(pose_payload_id=None, ligand_name="no pose", score=-1.0),
    ]
    out = tmp_path / "poses.sdf"

    assert export_poses_sdf(store.payloads, hits, out) == 2

    records = out.read_text().split("$$$$\n")[:-1]
    assert [r.splitlines()[0] for r in records] == ["aspirin", "caffeine"]
    assert "> <Score>\n-7.5\n" in records[0] and "> <Ligand_efficiency>\n0.4\n" in records[0]
    assert "Ligand_efficiency" not in records[1] and "> <Score>\n-6.1\n" in records[1]


def test_structures_leave_as_files_or_as_a_zip(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    items = [("rec 1", seed_payload(store, "ATOM A\n", "pdb")), ("rec 1", seed_payload(store, "ATOM B\n", "pdb")),
             ("lig", seed_payload(store, "ROOT\n", "pdbqt")), ("gone", 999)]

    assert export_structures(store.payloads, items, tmp_path / "files") == 3
    assert sorted(p.name for p in (tmp_path / "files").iterdir()) == ["lig.pdbqt", f"rec_1.pdb", f"rec_1_{items[1][1]}.pdb"]

    assert export_structures(store.payloads, items, tmp_path / "sel.zip") == 3
    with zipfile.ZipFile(tmp_path / "sel.zip") as archive:
        assert archive.read("lig.pdbqt") == b"ROOT\n"
        assert len(archive.namelist()) == 3
