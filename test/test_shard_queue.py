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

            def commit(self):
                pass

        yield _Session()


def _group(source, count, first_index=0):
    return {
        "source": source,
        "input_format": "sdf",
        "records": [(first_index + i, f"mol {source} {first_index + i}") for i in range(count)],
    }


def test_shards_hold_exactly_the_asked_size_whatever_the_chunks_were(tmp_path):
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)

    # Chunk sizes are the worker's business: 3, then 6, then 1 -> 10 records, 4 per shard.
    writer.handle("c0", [_group("lib.sdf", 3, 0)])
    writer.handle("c1", [_group("lib.sdf", 6, 3)])
    writer.handle("c2", [_group("lib.sdf", 1, 9)])
    assert [row.n_records for row in db.rows] == [4, 4]  # nothing partial before the end
    writer.flush()
    assert [row.n_records for row in db.rows] == [4, 4, 2]

    ids = []
    for row in db.rows:
        with Shard.open(row.path) as shard:
            ids.extend(record.id for record in shard)
            assert shard.metadata["source"] == "lib.sdf"
    assert ids == list(range(10))  # correlative across shards, not per shard


def test_provenance_survives_the_cut(tmp_path):
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=2)
    # A filter dropped 1 and 3: the ids close up, the source ordinals do not.
    writer.handle("c0", [{"source": "lib.sdf", "input_format": "sdf", "records": [(0, "a"), (2, "b")]}])
    writer.flush()

    with Shard.open(db.rows[0].path) as shard:
        assert shard.metadata["source_indices"] == [0, 2]
        assert [record.id for record in shard] == [0, 1]


def test_two_files_never_share_a_shard(tmp_path):
    db = _FakeDB()
    writer = ShardQueueWriter(project_db=db, shard_dir=tmp_path / "shards", shard_size=4)
    writer.handle("c0", [_group("a.sdf", 3), _group("b.sdf", 3)])
    writer.flush()

    assert {row.source for row in db.rows} == {"a.sdf", "b.sdf"}
    assert [row.n_records for row in db.rows] == [3, 3]
    assert len({row.shard_index for row in db.rows}) == 2  # the index is the queue's, not the file's
