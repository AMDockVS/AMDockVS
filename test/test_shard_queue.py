"""The queue that cuts shards: exact record counts, correlative ids, provenance kept.

The worker half is a filter with no state; this is the half with the arithmetic in it.
A file is one catalog row; its shards are the groups its header lists.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_flow.core.data import catalog
from ms_flow.core.data.shard import Shard, file_groups
from ms_flow.core.database import ProjectStore

from amdockvs.io.shards import ShardQueueWriter


def _db(tmp_path):
    db = ProjectStore()
    db.connect(tmp_path / "project")
    return db


def _files(db):
    return list(catalog.files(db))


def _shards(db):
    """Every shard of every file, ready or not, in file then id order."""
    return [SimpleNamespace(path=f["path"], file_id=f["id"], status=f["status"], base_id=g.base_id,
                            slot_count=g.slot_count, n_records=g.n_records, scope=g.scope)
            for f in _files(db) for g in (file_groups(f["path"]) if f["status"] == catalog.READY else [])]


def _group(batch, count, first_index=0):
    return {
        "import_batch_id": batch,
        "input_format": "sdf",
        "records": [(first_index + i, f"mol {batch} {first_index + i}") for i in range(count)],
    }


def _ids(row):
    return (row.base_id, row.base_id + row.slot_count)


def test_shards_hold_exactly_the_asked_size_whatever_the_chunks_were(tmp_path):
    db = _db(tmp_path)
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)

    # Chunk sizes are the worker's business: 3, then 6, then 1 -> 10 records, 4 per shard.
    writer.handle("c0", [_group(7, 3, 0)])
    writer.handle("c1", [_group(7, 6, 3)])
    writer.handle("c2", [_group(7, 1, 9)])
    # The file is still open: listed (a view watches it fill) but no shard of it is readable yet.
    assert [f["status"] for f in _files(db)] == [catalog.WRITING] and _shards(db) == []
    writer.flush()
    assert [row.n_records for row in _shards(db)] == [4, 4, 2]
    assert len({row.path for row in _shards(db)}) == 1  # one file, a row group each

    ids = []
    for row in _shards(db):
        with Shard.open(row.path, ids=_ids(row)) as shard:
            ids.extend(record.id for record in shard)
            assert len(shard) == row.n_records
    assert ids == list(range(10))  # correlative across shards, not per shard


def test_a_full_file_is_closed_and_its_rows_committed(tmp_path, monkeypatch):
    import amdockvs.io.shards as module

    monkeypatch.setattr(module, "MAX_RECORDS", 8)  # two shards of 4 per file
    db = _db(tmp_path)
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group(7, 12)])
    # The third shard opened a new file: the first is closed and ready, the second still open.
    assert [(f["status"], f["n_shards"]) for f in _files(db)] == [(catalog.READY, 2), (catalog.WRITING, 0)]
    writer.close()  # a stopped job keeps what it wrote
    assert [(f["status"], f["n_shards"], f["n_records"]) for f in _files(db)] == [
        (catalog.READY, 2, 8), (catalog.READY, 1, 4)]


def test_provenance_survives_the_cut(tmp_path):
    import pyarrow.parquet as pq

    db = _db(tmp_path)
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=2)
    # A filter dropped 1 and 3: the ids close up, the source ordinals do not.
    writer.handle("c0", [{"import_batch_id": 7, "input_format": "sdf", "records": [(0, "a"), (2, "b")]}])
    writer.flush()

    with Shard.open(_shards(db)[0].path, ids=_ids(_shards(db)[0])) as shard:
        assert [record.id for record in shard] == [0, 1]
    assert pq.read_table(_shards(db)[0].path, columns=["source_index"]).column(0).to_pylist() == [0, 2]


def test_two_files_never_share_a_shard(tmp_path):
    """...but they share the file: a library of 168 sources is a few big files, not 168."""
    db = _db(tmp_path)
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group(1, 3), _group(2, 3)])
    writer.flush()

    assert {row.scope for row in _shards(db)} == {1, 2}
    assert [row.n_records for row in _shards(db)] == [3, 3]
    assert len({row.path for row in _shards(db)}) == 1


def test_a_partial_shard_left_for_the_end_rolls_into_a_new_file(tmp_path, monkeypatch):
    """The import that died at flush: a source's last partial shard drew its ids after every
    other source had moved the counter past the open file's window."""
    import amdockvs.io.shards as module

    monkeypatch.setattr(module, "MAX_RECORDS", 8)
    db = _db(tmp_path)
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group(1, 4), _group(2, 6)])  # ids 0-3 (1) and 4-7 (2); 2 of batch 2 wait
    writer.handle("c1", [_group(1, 3, 4)])  # 3 of batch 1 wait
    writer.flush()  # ids 8-10 and 11-12: past [0, 8), so a second file

    assert [(row.scope, row.base_id, row.n_records) for row in _shards(db)] == [
        (1, 0, 4), (2, 4, 4), (1, 8, 3), (2, 11, 2)]
    assert len({row.path for row in _shards(db)}) == 2
    for row in _shards(db):
        with Shard.open(row.path, ids=_ids(row)) as shard:
            assert [record.load().split()[1] for record in shard] == [str(row.scope)] * row.n_records


def test_a_second_import_does_not_overwrite_the_first(tmp_path):
    """Each import restarts its ids at 0 in the same folder: file names are unique."""
    db = _db(tmp_path)
    for batch in (1, 2):
        writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
        writer.handle("c0", [_group(batch, 2)])
        writer.flush()

    assert [row.base_id for row in _shards(db)] == [0, 0]  # ids restart per import
    assert len({row.path for row in _shards(db)}) == 2
    for row in _shards(db):
        with Shard.open(row.path, ids=_ids(row)) as shard:
            assert [record.load() for record in shard] == [f"mol {row.scope} {i}" for i in range(2)]
