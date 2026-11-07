"""Ligand rows on a cluster's queue: their chunks travel with the ligands in transport shards, and
come back there as the very chunks `docking_job` runs here."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_flow.engine import Dir, File
from ms_flow.runner import unplain


def _chunks(receptor: File, out: Dir) -> list[dict]:
    def pair(ligand_id, receptor_id):
        return {"ligand_id": ligand_id, "receptor_id": receptor_id, "ligand": f"PDBQT {ligand_id}".encode(),
                "receptor_path": receptor, "box_center": [1.0, 2.0, 3.0], "num_modes": 9,
                "ligand_descriptors": {"mw": float(ligand_id)}}
    failed = {"ligand_id": 4, "receptor_id": 20, "invalid_reason": "no grid"}
    # Receptor-major, as the feed docks: the second receptor's ligands start again from the lowest id.
    return [{"pairs": [pair(1, 10), pair(2, 10)], "engine": "vina", "output_dir": out, "docking_run_id": 7},
            {"pairs": [pair(3, 10), pair(1, 20)], "engine": "vina", "output_dir": out, "docking_run_id": 7},
            {"pairs": [pair(2, 20), failed], "engine": "vina", "output_dir": out, "docking_run_id": 7}]


def test_rows_travel_in_shards_and_are_the_same_chunks_there(tmp_path, monkeypatch):
    import amdockvs.docking.campaign as campaign
    import amdockvs.docking.tasks as tasks

    receptor = tmp_path / "r.pdbqt"
    receptor.write_text("RECEPTOR")
    chunks = _chunks(File(str(receptor)), Dir(str(tmp_path / "out")))
    monkeypatch.setattr(campaign, "TRANSPORT_RECORDS", 2)
    manifest = campaign.pack_rows([dict(chunk) for chunk in _chunks(File(str(receptor)), Dir(str(tmp_path / "out")))],
                                  tmp_path / "transport", cpu=2)
    again = campaign.pack_rows(_chunks(File(str(receptor)), Dir(str(tmp_path / "out"))), tmp_path / "transport", cpu=2)
    assert again == manifest  # the same ligands make the same files: a campaign submitted again is found
    shards = sorted((tmp_path / "transport").glob("*.parquet"))
    assert len(shards) == 2 and not list((tmp_path / "transport").glob(".packing-*"))  # 1+2, then 3: each ligand once

    written = json.loads(manifest.read_text())
    assert set(written["digests"]) == {str(path) for path in shards}
    elements = unplain(written["elements"])
    assert all(element["_cpu_required"] == 2 for element in elements)
    assert "receptor_path" in elements[0]["common"]  # what the pairs share is said once

    seen = []
    monkeypatch.setattr(tasks, "dock_with_interactions", lambda chunk: seen.append(chunk) or {})
    for element in elements:
        tasks.dock_rows(element)
    assert [chunk["pairs"] for chunk in seen] == [chunk["pairs"] for chunk in chunks]
    assert [chunk["docking_run_id"] for chunk in seen] == [7, 7, 7]


def test_an_ended_campaign_takes_its_transport_away_but_not_what_another_still_names(tmp_path, monkeypatch):
    import amdockvs.docking.campaign as campaign

    receptor = tmp_path / "r.pdbqt"
    receptor.write_text("RECEPTOR")
    monkeypatch.setattr(campaign, "TRANSPORT_RECORDS", 2)
    folder = tmp_path / "transport"
    first = campaign.pack_rows(_chunks(File(str(receptor)), Dir(str(tmp_path / "out"))), folder, cpu=1)
    second = campaign.pack_rows(_chunks(File(str(receptor)), Dir(str(tmp_path / "out")))[:1], folder, cpu=1)
    shared = set(json.loads(second.read_text())["digests"])
    assert shared < set(json.loads(first.read_text())["digests"])  # ligands 1+2: the same file

    campaign.drop_transport(first)
    assert {str(path) for path in folder.glob("*.parquet")} == shared and second.exists()
    campaign.drop_transport(second)
    assert not list(folder.iterdir())


def test_a_stopped_campaign_keeps_its_transport_for_resume(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import amdockvs.docking.campaign as campaign

    receptor = tmp_path / "r.pdbqt"
    receptor.write_text("RECEPTOR")
    manifest = campaign.pack_rows(_chunks(File(str(receptor)), Dir(str(tmp_path / "out"))), tmp_path / "t", cpu=1)
    assert len(manifest.stem) == 16

    class Project:
        pass

    project = Project()
    monkeypatch.setitem(campaign.configs, project, {"project_db": None})
    monkeypatch.setattr(campaign, "_supersede", lambda *_: None)
    params = SimpleNamespace(elements=str(manifest), model_dump=lambda mode: {})
    for status in ("canceled", "failed"):
        campaign._rows_ended(params, project, SimpleNamespace(status=status))
        assert manifest.exists()  # Resume reads it again
    campaign._rows_ended(params, project, SimpleNamespace(status="completed"))
    assert not list((tmp_path / "t").iterdir())


def test_clearing_the_queue_files_by_hand_frees_what_a_stopped_campaign_kept(tmp_path, monkeypatch):
    import amdockvs.docking.campaign as campaign

    receptor = tmp_path / "r.pdbqt"
    receptor.write_text("RECEPTOR")
    folder = tmp_path / "t"
    campaign.pack_rows(_chunks(File(str(receptor)), Dir(str(tmp_path / "out"))), folder, cpu=1)
    size = sum(path.stat().st_size for path in folder.iterdir())
    monkeypatch.setattr(campaign, "transport_folder", lambda _db: folder)
    assert campaign.clear_transport(None) == size > 0
    assert not list(folder.iterdir())
    assert campaign.clear_transport(None) == 0  # nothing left, or never packed
