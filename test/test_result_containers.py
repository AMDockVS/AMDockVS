"""M6a: a key's results are two containers, the summary (what is ranked) and the payload (poses).
A summary read never opens the payload."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _docked(project_db, folder, *, key, shard_id, run, scores):
    """One shard docked for `key` by run `run`: its two parts written and recorded."""
    from amdockvs.docking.results.dataset import ResultFragmentWriter, result_row
    from ms_flow.core.data import catalog

    from amdockvs.screening.campaign import record_docked

    path = Path(folder) / "parts" / f"shard_{shard_id}-{run}.arrows"
    with ResultFragmentWriter(path, metadata={"input_key": "in", "run_id": f"run-{run}"}, origin=shard_id) as writer:
        for ligand_id, score in scores.items():
            writer.add(result_row(
                {"ligand_molecule_id": ligand_id, "receptor_molecule_id": 5, "score": score},
                smiles="C", engine="vina", shard_id=shard_id, docking_run_id=run,
            ) | {"pose_text": f"MODEL {run}\n", "pose_suffix": ".pdbqt", "has_pose": True})
    record_docked(project_db, key=key, shard_id=shard_id, result_path=str(path), part=f"run-{run}:{shard_id}",
                  generation_id=catalog.active_generation_id(project_db), force=True)


def _library(project_db, folder):
    """Two library shards, the ones docked: a hit names the shard it is of."""
    import pyarrow as pa
    from ms_flow.core.data import catalog
    from ms_flow.core.data.shard import write_table

    generation = catalog.create_generation(project_db, step="import")
    catalog.activate_generation(project_db, generation)
    with catalog.ContainerWriter(project_db, folder, generation_id=generation) as writer:
        receipts = writer.commit_parts([
            {"part": f"lib:{n}", "path": str(write_table(Path(folder) / "parts" / f"{n}.arrows", pa.table({"id": [0, 1]})))}
            for n in range(2)])
    return [receipt["shards"][0] for receipt in receipts]


def test_a_summary_read_opens_no_payload_container(tmp_path, monkeypatch):
    from ms_flow.core.data import container
    from ms_flow.core.database import ProjectStore

    from amdockvs.docking.results import dataset

    db = ProjectStore()
    db.connect(tmp_path / "project")
    try:
        key = "dock-k"
        a, b = _library(db, tmp_path / "library")
        _docked(db, tmp_path / "results", key=key, shard_id=a, run=1, scores={0: -7.0, 1: -9.0})
        _docked(db, tmp_path / "results", key=key, shard_id=b, run=1, scores={0: -8.0})
        _docked(db, tmp_path / "results", key=key, shard_id=a, run=2, scores={1: -6.0})  # a redo of one pair
        payloads = {str(path) for path in dataset.key_files(db, [key], role=dataset.payload_role)}
        assert payloads
        opened = []
        read_table = container.read_table
        monkeypatch.setattr(container, "read_table", lambda path, *a, **k: opened.append(str(path)) or read_table(path, *a, **k))

        refs = dataset.select_hit_refs(db, keys=[key], top_n=10)
        assert [(r["_shard_id"], r["ligand_id"], r["score"]) for r in refs] == [(b, 0, -8.0), (a, 0, -7.0), (a, 1, -6.0)]
        assert dataset.docked_pairs(db, [key]) == {(1, a), (1, b), (2, a)}
        assert opened and not set(opened) & payloads

        hits = dataset.select_hits(db, keys=[key], top_n=1)  # the poses: the payload of their own attempt
        assert set(opened) & payloads
        assert [(h["score"], h["_pose_text"], h["input_key"]) for h in hits] == [(-8.0, "MODEL 1\n", "in")]
    finally:
        db.disconnect()


def test_the_arrow_top_n_keeps_the_order_of_the_python_gate():
    import random

    import pyarrow as pa

    from amdockvs.docking.results.dataset import _top
    from amdockvs.screening.materialize import TopNGate

    rng = random.Random(7)
    rows = [{"ligand_id": i, "score": rng.choice([None, -9.0, -8.5, -8.5, -7.0, rng.uniform(-10, -5)]),
             "has_pose": rng.random() > 0.1} for i in range(500)]
    gate = TopNGate(cap=40, threshold=None)
    gate.take(row for row in rows if row["score"] is not None and row["has_pose"])
    assert [row["ligand_id"] for row in _top(pa.Table.from_pylist(rows), 40).to_pylist()] == \
        [row["ligand_id"] for row in gate.drain()]
