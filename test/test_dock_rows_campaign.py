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
