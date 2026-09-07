"""The unit of sharded work is the shard: a chunk carries exactly one whole shard.

What is worth checking is that a shard is never grouped or split across chunks — which is what lets
`chunks_total` be "one per receptor" and the shard state flip to `docked` on its own.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.docking import shard_jobs
from amdockvs.docking.preparation import jobs as preparation_jobs


def _shard_rows(tmp_path, count, *, fmt="smi"):
    rows = []
    for index in range(count):
        path = tmp_path / f"shard_{index:05d}.{fmt}"
        path.write_text("x")
        rows.append({
            "id": index + 1, "source": str(tmp_path / "lib.smi"), "shard_index": index, "path": str(path),
            "prepared_path": str(path),
            "input_format": fmt, "n_records": 1000, "state": "prepared",
        })
    return rows


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self, project_db, **_kw):
        return self

    def iter_rows(self, *_a, **_kw):
        yield from self._rows


class _EmptySession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def exec(self, _statement):
        return self

    def all(self):
        return []


class _ProjectDB:
    def get_session(self):
        return _EmptySession()


def test_preparation_sends_one_whole_shard_per_task(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation_jobs, "ShardStore", _Store(_shard_rows(tmp_path, 5)))
    chunks = list(preparation_jobs.PrepareLigandShardsJobSpec.build_chunks(
        {"engine": "ad4"}, {"project_db": _ProjectDB()}
    ))
    assert [len(chunk["shards"]) for chunk in chunks] == [1, 1, 1, 1, 1]
    assert [s["shard_index"] for chunk in chunks for s in chunk["shards"]] == [0, 1, 2, 3, 4]
    # Nothing pending still yields one chunk so the job can close; it does no work.
    monkeypatch.setattr(preparation_jobs, "ShardStore", _Store([]))
    empty = list(preparation_jobs.PrepareLigandShardsJobSpec.build_chunks({}, {"project_db": _ProjectDB()}))
    assert empty == [{"shards": [], "engine": "ad4"}]
    assert preparation_jobs.PrepareLigandShardsJobSpec.run_chunk(empty[0]) == []


def test_docking_sends_one_whole_shard_per_receptor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shard_jobs,
        "iter_prepared_shards",
        lambda *_a, **_kw: iter(_shard_rows(tmp_path, 3, fmt="pdbqt")),
    )
    monkeypatch.setattr(shard_jobs, "completed_shard_targets", lambda *a, **k: set())
    monkeypatch.setattr(shard_jobs, "count_docking_results", lambda *a, **k: 0)
    monkeypatch.setattr(shard_jobs, "receptor_targets", lambda *a, **k: [
        {"receptor_id": r, "receptor_name": f"r{r}", "receptor_path": str(tmp_path / f"r{r}.pdbqt"),
         "box_center": [0.0, 0.0, 0.0], "box_size": [20.0, 20.0, 20.0], "spacing": 0.375}
        for r in (7, 8)
    ])
    params = {"output_dir": str(tmp_path), "hit_threshold": -8.0, "hit_cap": 100, "batch_size": 1}
    assert shard_jobs.DockShardsJobSpec.count_chunks(params, {"project_db": object()}) == 6
    chunks = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": object()}))
    assert len(chunks) == 6  # 3 shards x 2 receptors
    assert all(len(chunk["shards"]) == 1 for chunk in chunks)
    # Every receptor sees the whole shard exactly once, so this many chunks close it out.
    assert {chunk["chunks_total"] for chunk in chunks} == {2}
    assert sorted(chunk["receptor_id"] for chunk in chunks) == [7, 7, 7, 8, 8, 8]
    # No slice bounds travel any more: the shard is the slice.
    assert not any("start" in chunk or "count" in chunk for chunk in chunks)

    # Resuming the same run skips only the completed shard/receptor pair, not the shard in every
    # receptor or an unrelated campaign.
    monkeypatch.setattr(shard_jobs, "completed_shard_targets", lambda *a, **k: {(1, 7)})
    assert shard_jobs.DockShardsJobSpec.count_chunks(params, {"project_db": object()}) == 5
    resumed = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": object()}))
    pairs = [
        (shard["shard_id"], chunk["receptor_id"])
        for chunk in resumed
        for shard in chunk["shards"]
    ]
    assert (1, 7) not in pairs
    assert sorted(pairs) == [(1, 8), (2, 7), (2, 8), (3, 7), (3, 8)]


def test_off_target_scope_uses_only_reference_top_n_records(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shard_jobs,
        "iter_prepared_shards",
        lambda *_a, **_kw: iter(_shard_rows(tmp_path, 3, fmt="pdbqt")),
    )
    monkeypatch.setattr(shard_jobs, "get_default_project_root", lambda: tmp_path)
    monkeypatch.setattr(shard_jobs, "completed_shard_targets", lambda *a, **k: set())
    monkeypatch.setattr(shard_jobs, "plan_targets", lambda *a, **k: None)
    monkeypatch.setattr(shard_jobs, "select_hit_refs", lambda *a, **k: [
        {"_shard_id": 1, "ligand_id": 3},
        {"_shard_id": 3, "ligand_id": 200},
        {"_shard_id": 3, "ligand_id": 201},
    ])
    monkeypatch.setattr(shard_jobs, "receptor_targets", lambda *a, **k: [
        {"receptor_id": receptor_id, "receptor_name": f"R{receptor_id}",
         "receptor_path": str(tmp_path / f"r{receptor_id}.pdbqt"),
         "box_center": [0.0, 0.0, 0.0], "box_size": [20.0, 20.0, 20.0], "spacing": 0.375}
        for receptor_id in (8, 9)
    ])
    params = {
        "output_dir": str(tmp_path),
        "hit_threshold": 0.0,
        "hit_cap": 3,
        "hit_mode": "top_n",
        "run_id": "run",
        "selected_from_receptor_id": 7,
        "selected_top_n": 3,
    }

    assert shard_jobs.DockShardsJobSpec.count_chunks(params, {"project_db": object()}) == 4
    chunks = list(shard_jobs.DockShardsJobSpec.build_chunks(params, {"project_db": object()}))
    assert len(chunks) == 4
    selected = {
        chunk["shards"][0]["shard_id"]: chunk["shards"][0]["record_ids"]
        for chunk in chunks
    }
    assert selected == {1: [3], 3: [200, 201]}
    assert {chunk["shards"][0]["n_records"] for chunk in chunks} == {1, 2}
