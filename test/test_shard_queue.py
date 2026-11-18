"""The queue that cuts shards: exact record counts, correlative ids, provenance kept.

The worker half is a filter with no state; this is the half with the arithmetic in it.
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ms_flow.core.data.shard import Shard

from amdockvs.io.shards import ShardQueueWriter


class _FakeDB:
    """Enough of a project db to catch the inventory rows."""

    def __init__(self):
        self.rows = []

    @contextmanager
    def get_session(self):
        db = self

        class _Session:
            def add(self, row):
                db.rows.append(row)

            def add_all(self, rows):
                for row in rows:
                    self.add(row)

            def commit(self):
                pass

        yield _Session()


def _group(batch, count, first_index=0):
    return {
        "import_batch_id": batch,
        "input_format": "sdf",
        "records": [(first_index + i, f"mol {batch} {first_index + i}") for i in range(count)],
    }


def _ids(row):
    return (row.base_id, row.base_id + row.slot_count)


def test_shards_hold_exactly_the_asked_size_whatever_the_chunks_were(tmp_path):
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)

    # Chunk sizes are the worker's business: 3, then 6, then 1 -> 10 records, 4 per shard.
    writer.handle("c0", [_group(7, 3, 0)])
    writer.handle("c1", [_group(7, 6, 3)])
    writer.handle("c2", [_group(7, 1, 9)])
    assert db.rows == []  # the file is still open: no row may point at it yet
    writer.flush()
    assert [row.n_records for row in db.rows] == [4, 4, 2]
    assert len({row.path for row in db.rows}) == 1  # one file, a row group each

    ids = []
    for row in db.rows:
        with Shard.open(row.path, ids=_ids(row)) as shard:
            ids.extend(record.id for record in shard)
            assert shard.metadata["import_batch_id"] == 7
            assert len(shard) == row.n_records
    assert ids == list(range(10))  # correlative across shards, not per shard


def test_a_full_file_is_closed_and_its_rows_committed(tmp_path, monkeypatch):
    import amdockvs.io.shards as module

    monkeypatch.setattr(module, "MAX_RECORDS", 8)  # two shards of 4 per file
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group(7, 12)])
    assert [row.shard_index for row in db.rows] == [0, 1]  # the third shard opened a new file
    writer.close()  # a stopped job keeps what it wrote
    assert [row.shard_index for row in db.rows] == [0, 1, 2]
    assert len({row.path for row in db.rows}) == 2


def test_provenance_survives_the_cut(tmp_path):
    import pyarrow.parquet as pq

    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=2)
    # A filter dropped 1 and 3: the ids close up, the source ordinals do not.
    writer.handle("c0", [{"import_batch_id": 7, "input_format": "sdf", "records": [(0, "a"), (2, "b")]}])
    writer.flush()

    with Shard.open(db.rows[0].path, ids=_ids(db.rows[0])) as shard:
        assert [record.id for record in shard] == [0, 1]
    assert pq.read_table(db.rows[0].path, columns=["source_index"]).column(0).to_pylist() == [0, 2]


def test_two_files_never_share_a_shard(tmp_path):
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group(1, 3), _group(2, 3)])
    writer.flush()

    assert {row.import_batch_id for row in db.rows} == {1, 2}
    assert [row.n_records for row in db.rows] == [3, 3]
    assert len({row.shard_index for row in db.rows}) == 2  # the index is the queue's, not the file's
    assert len({row.path for row in db.rows}) == 2  # a file per batch


def test_a_second_import_does_not_overwrite_the_first(tmp_path):
    """Each import restarts its ids at 0 in the same folder: file names are unique."""
    db = _FakeDB()
    for batch in (1, 2):
        writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
        writer.handle("c0", [_group(batch, 2)])
        writer.flush()

    assert [row.shard_index for row in db.rows] == [0, 0]
    assert len({row.path for row in db.rows}) == 2
    for row in db.rows:
        with Shard.open(row.path, ids=_ids(row)) as shard:
            assert shard.metadata["import_batch_id"] == row.import_batch_id
