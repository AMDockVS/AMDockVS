"""Rescoring: the pose score replaced (with the docking) and poses rescored from the database."""
from __future__ import annotations

import pytest

from amdockvs.docking import rescoring


def _sdf(name: str) -> str:
    return f"{name}\n     RDKit          3D\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n$$$$\n"


def test_sdf_records_splits_on_the_terminator():
    text = _sdf("pose one") + _sdf("")  # the second molecule's name line is blank
    records = rescoring.sdf_records(text)
    assert len(records) == 2 and all(record.endswith("$$$$\n") for record in records)


def test_rescoring_with_the_docking_replaces_the_score_and_reranks(tmp_path, monkeypatch):
    poses = tmp_path / "pair.sdf"
    poses.write_text(_sdf("rank1") + _sdf("rank2"), encoding="utf-8")
    scores = {1: -5.0, 2: -9.0}  # the second pose is the better one for the new function
    monkeypatch.setattr(rescoring, "rescore_pose",
                        lambda **kw: scores[int(kw["pose_path"].stem.rsplit("_", 1)[1])])
    rows = [{"receptor_molecule_id": 1, "ligand_molecule_id": 2, "pose_rank": rank,
             "score": -8.0 - rank, "score_type": "vina", "pose_path": str(poses),
             "ligand_efficiency": -0.5} for rank in (1, 2)]

    out = rescoring.rescore_rows(rows, function="vinardo", pairs=[
        {"receptor_id": 1, "ligand_id": 2, "receptor_path": "r.pdbqt",
         "box_center": [0.0, 0.0, 0.0], "box_size": [20.0, 20.0, 20.0],
         "ligand_descriptors": {"heavy_atoms": 20, "clogp": 2.0}}])

    by_rank = {row["pose_rank"]: row for row in out}
    assert by_rank[1]["score"] == -9.0 and by_rank[2]["score"] == -5.0
    assert by_rank[1]["score_type"] == "vinardo_rescore"
    # The metrics are the new score's, not the search score's.
    assert by_rank[1]["ligand_efficiency"] == -9.0 / 20
    assert not list(tmp_path.glob("*_rescore_*.sdf"))  # the single-pose files are cleaned up


@pytest.mark.parametrize("storage", ["payload", "fragment"])
@pytest.mark.parametrize("failed", [None, "docking", "rescoring"])
def test_reranking_keeps_each_score_with_its_original_pose(tmp_path, monkeypatch, storage, failed):
    poses = tmp_path / "pair.sdf"
    poses.write_text(_sdf("pose_A") + _sdf("pose_B"), encoding="utf-8")

    def score(**kwargs):
        if kwargs["pose_path"].read_text().startswith("pose_A"):
            if failed == "rescoring":
                raise RuntimeError("pose A cannot be scored")
            return -5.0
        return -9.0

    monkeypatch.setattr(rescoring, "rescore_pose", score)
    rows = [{"receptor_molecule_id": 1, "ligand_molecule_id": 2, "pose_rank": rank,
             "score": None if rank == 1 and failed == "docking" else -10.0 + rank,
             "pose_path": str(poses), "extra": {"rmsd_method": "alignment"}}
            for rank in (1, 2)]
    rows = rescoring.rescore_rows(rows, function="vinardo", pairs=[{"receptor_id": 1, "ligand_id": 2}])
    rows.sort(key=lambda row: row["pose_rank"])

    if storage == "payload":
        from amdockvs.docking.tasks import _poses_as_payloads

        stored = _poses_as_payloads(rows)
        texts = [row["pose_payload_id"].data.decode() for row in stored]
        assert not poses.exists()
    else:
        from amdockvs.docking.results.dataset import result_row

        stored = [result_row(row, smiles="") for row in rows]
        texts = [row["pose_text"] for row in stored]

    assert [row["score"] for row in stored] == [-9.0, None if failed else -5.0]
    assert texts == [_sdf("pose_B"), _sdf("pose_A")]
    assert all(row["extra"]["rmsd_method"] == "alignment" for row in rows)
    if failed == "rescoring":
        assert "pose A cannot be scored" in rows[1]["error"]


def test_rescoring_stored_poses_writes_rows_of_the_rescoring_run(tmp_path, monkeypatch):
    pose = tmp_path / "pose.sdf"
    pose.write_text(_sdf("stored"), encoding="utf-8")
    monkeypatch.setattr(rescoring, "rescore_pose", lambda **kw: -7.5)

    rows = rescoring.rescore_stored_poses({
        "function": "vinardo", "docking_run_id": 42,
        "rows": [{"id": 3, "receptor_molecule_id": 1, "ligand_molecule_id": 2, "pose_rank": 1,
                  "pose_path": str(pose), "pose_payload_id": 9, "receptor_payload_id": 8,
                  "receptor_path": "r.pdbqt", "input_key": "abc", "binding_site_id": 5,
                  "box_center": [0.0, 0.0, 0.0], "box_size": [20.0, 20.0, 20.0]}],
    })

    assert len(rows) == 1
    row = rows[0]
    assert row["docking_run_id"] == 42 and row["score"] == -7.5
    assert row["score_type"] == "vinardo_rescore" and row["binding_site_id"] == 5
    # The same stored pose: the rescoring does not re-dock, and the docking row stays as it is.
    assert row["pose_payload_id"] == 9 and row["input_key"] == "abc" and "pose_path" not in row


def test_a_pose_the_function_cannot_score_keeps_no_score(tmp_path, monkeypatch):
    pose = tmp_path / "pose.sdf"
    pose.write_text(_sdf("stored"), encoding="utf-8")

    def fail(**_kw):
        raise RuntimeError("vina said no")

    monkeypatch.setattr(rescoring, "rescore_pose", fail)
    rows = rescoring.rescore_stored_poses({
        "function": "vina", "docking_run_id": 1,
        "rows": [{"receptor_molecule_id": 1, "ligand_molecule_id": 2, "pose_path": str(pose)}]})
    assert rows[0]["score"] is None and "vina said no" in rows[0]["error"]


def test_the_rescoring_job_plans_a_run_of_its_own_over_the_stored_poses(tmp_path):
    """The job side: a new run whose protocol is the source one plus the function, chunks carrying
    the site's box, and a pose with no box left out."""
    from ms_flow.core.database.project import ProjectStore
    from payload_seed import seed_payload
    from sqlmodel import select

    from amdockvs.core.constants import SERIES_POSES
    from amdockvs.docking.rescore_jobs import RescoreJobSpec
    from amdockvs.docking.results.repository import docking_run_id
    from amdockvs.models.docking import BindingSite, DockingResult, DockingRun
    from amdockvs.models.molecules import MoleculeRecord

    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        receptor = MoleculeRecord(name="rec", is_receptor=True)
        ligand = MoleculeRecord(name="lig", is_ligand=True)
        session.add_all([receptor, ligand])
        session.commit()
        boxed = BindingSite(molecule_id=receptor.id, is_active=True, center_x=1.0, center_y=2.0,
                            center_z=3.0, size_x=20.0, size_y=20.0, size_z=20.0)
        empty = BindingSite(molecule_id=receptor.id, name="undefined")
        session.add_all([boxed, empty])
        session.commit()
        run = docking_run_id(store, run_id="src", engine="vina", run_kind="screening",
                             protocol={"hash": "h", "program": "vina", "label": "Vina"})
        for site in (boxed.id, empty.id):
            session.add(DockingResult(
                receptor_molecule_id=receptor.id, ligand_molecule_id=ligand.id, docking_run_id=run,
                binding_site_id=site, pose_rank=1, score=-8.0,
                pose_payload_id=seed_payload(store, f"pose {site}\n", "sdf", SERIES_POSES)))
        session.commit()

    chunks = list(RescoreJobSpec.build_chunks({"function": "vinardo"}, {"project_db": store}))

    assert len(chunks) == 1 and len(chunks[0]["rows"]) == 1  # the site with no box was skipped
    row = chunks[0]["rows"][0]
    assert row["box_center"] == [1.0, 2.0, 3.0] and str(row["pose_path"]).endswith(".sdf")
    with store.get_session() as session:
        runs = {r.id: r for r in session.exec(select(DockingRun)).all()}
    new = runs[chunks[0]["docking_run_id"]]
    assert new.id != run and new.protocol["rescoring"] == [{"program": "vinardo"}]
    assert new.protocol["hash"] != "h" and new.protocol["label"] == "Vina + vinardo"
    store.dispose()
