"""M6a: a key's results are two containers, the summary (what is ranked) and the payload (poses).
A summary read never opens the payload."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _docked(project_db, folder, *, key, shard_id, run, scores, failed=(), part=None):
    """One shard docked for `key` by run `run`: its two parts written and recorded."""
    from amdockvs.docking.results.dataset import ResultFragmentWriter, result_row
    from ms_flow.core.data import catalog

    from amdockvs.screening.campaign import record_docked

    path = Path(folder) / "parts" / f"shard_{shard_id}-{run}-{len(list(Path(folder).glob('parts/*')))}.arrows"
    with ResultFragmentWriter(path, metadata={"input_key": "in", "run_id": f"run-{run}"}, origin=shard_id) as writer:
        for ligand_id, score in scores.items():
            writer.add(result_row(
                {"ligand_molecule_id": ligand_id, "receptor_molecule_id": 5, "score": score,
                 **({"error": "the engine failed"} if ligand_id in failed else {})},
                smiles="C", engine="vina", shard_id=shard_id, docking_run_id=run,
            ) | {"pose_text": f"MODEL {run}\n", "pose_suffix": ".pdbqt", "has_pose": True})
    record_docked(project_db, key=key, shard_id=shard_id, result_path=str(path), part=part or f"run-{run}:{shard_id}",
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


def test_the_arrow_top_n_is_the_best_scores_a_tie_to_the_later_row():
    import random

    import pyarrow as pa

    from amdockvs.docking.results.dataset import _top

    rng = random.Random(7)
    rows = [{"ligand_id": i, "score": rng.choice([None, -9.0, -8.5, -8.5, -7.0, rng.uniform(-10, -5)]),
             "has_pose": rng.random() > 0.1} for i in range(500)]
    best = sorted((row for row in rows if row["score"] is not None and row["has_pose"]),
                  key=lambda row: (row["score"], -row["ligand_id"]))[:40]
    assert [row["ligand_id"] for row in _top(pa.Table.from_pylist(rows), 40).to_pylist()] == \
        [row["ligand_id"] for row in best]


def _project(tmp_path):
    from ms_flow.core.database import ProjectStore

    db = ProjectStore()
    db.connect(tmp_path / "project")
    return db


def _retire(db, folder, key):
    """What a run's finish does to a key's containers (`ShardHitWriter.close`)."""
    from ms_flow.core.data import catalog

    from amdockvs.docking.results.dataset import UNIQUE_BY, result_roles

    with catalog.PairWriter(db, folder, generation_id=catalog.active_generation_id(db),
                            roles=result_roles(key)) as writer:
        return writer.retire_covered(UNIQUE_BY[-1])


def _ranked(db, key, **filters):
    from amdockvs.docking.results import dataset

    return [(r["_shard_id"], r["ligand_id"], r["score"])
            for r in dataset.select_hit_refs(db, keys=[key], filters=filters, top_n=10)]


def test_a_narrowed_redo_keeps_the_other_records(tmp_path):
    db = _project(tmp_path)
    try:
        a, _b = _library(db, tmp_path / "library")
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=1, scores={0: -7.0, 1: -9.0})
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=2, scores={1: -6.0})  # narrowed to one
        assert _retire(db, tmp_path / "results", "k") == []  # record 0 is the whole run's alone
        assert _ranked(db, "k") == [(a, 0, -7.0), (a, 1, -6.0)]
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=3, scores={0: -5.0})
        assert len(_retire(db, tmp_path / "results", "k")) == 1  # now 1 and 0 are newer: run 1 goes
        assert _ranked(db, "k") == [(a, 1, -6.0), (a, 0, -5.0)]
    finally:
        db.disconnect()


def test_a_replay_is_one_row(tmp_path):
    db = _project(tmp_path)
    try:
        a, _b = _library(db, tmp_path / "library")
        for score in (-7.0, -8.0):  # the same task twice: the first receipt wins (I10)
            _docked(db, tmp_path / "results", key="k", shard_id=a, run=1, scores={0: score}, part="run-1:a")
        assert _ranked(db, "k") == [(a, 0, -7.0)]
    finally:
        db.disconnect()


def test_a_failed_redo_hides_the_old_result(tmp_path):
    db = _project(tmp_path)
    try:
        a, _b = _library(db, tmp_path / "library")
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=1, scores={0: -9.0, 1: -8.0})
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=2, scores={0: None}, failed={0})
        assert _ranked(db, "k") == [(a, 1, -8.0)]
    finally:
        db.disconnect()


def test_a_redo_below_the_threshold_hides_the_old_result(tmp_path):
    db = _project(tmp_path)
    try:
        a, _b = _library(db, tmp_path / "library")
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=1, scores={0: -9.0})
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=2, scores={0: -5.0})
        assert _ranked(db, "k", score__lte=-8.0) == []
    finally:
        db.disconnect()


def test_only_origins_docked_twice_are_deduplicated(tmp_path, monkeypatch):
    """The index alone tells which origins repeat: the others never reach the dedupe."""
    from amdockvs.docking.results import dataset

    db = _project(tmp_path)
    try:
        a, b = _library(db, tmp_path / "library")
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=1, scores={0: -7.0, 1: -9.0})
        _docked(db, tmp_path / "results", key="k", shard_id=b, run=1, scores={0: -8.0, 1: -4.0})
        _docked(db, tmp_path / "results", key="k", shard_id=a, run=2, scores={1: -6.0})
        seen = []
        newest = dataset._newest
        monkeypatch.setattr(dataset, "_newest", lambda table: seen.append(table.num_rows) or newest(table))
        assert _ranked(db, "k") == [(b, 0, -8.0), (a, 0, -7.0), (a, 1, -6.0), (b, 1, -4.0)]
        assert seen == [3]  # a's two shards, not b's
    finally:
        db.disconnect()


def test_the_top_n_streams_containers_and_dedups_a_redo_across_sealed_ones(tmp_path, monkeypatch):
    """Each container's best are kept as it is read; a pair docked again in a later container
    still hides its old row there."""
    import random

    from ms_flow.core.data import catalog

    from amdockvs.docking.results import dataset

    monkeypatch.setattr(catalog, "MAX_SHARDS", 1)  # a container per docked shard
    rng = random.Random(3)
    db = _project(tmp_path)
    try:
        a, b = _library(db, tmp_path / "library")
        newest = {}
        for run in (1, 2, 3):
            for shard in (a, b):
                # each run scores worse than the one before: an old row that leaks ranks first
                scores = {ligand: rng.uniform(-10, -9) + 1.5 * run for ligand in rng.sample(range(6), 3)}
                _docked(db, tmp_path / "results", key="k", shard_id=shard, run=run, scores=scores)
                newest.update({(shard, ligand): score for ligand, score in scores.items()})
        assert len(dataset.key_files(db, ["k"])) > 2
        best = sorted(((shard, ligand, score) for (shard, ligand), score in newest.items()), key=lambda r: r[2])
        assert [(r["_shard_id"], r["ligand_id"], r["score"])
                for r in dataset.select_hit_refs(db, keys=["k"], top_n=4)] == best[:4]
        assert len(dataset.hit_refs(db, keys=["k"], top_n=4)["payloads"]) <= 4  # metas of the winners only
    finally:
        db.disconnect()
