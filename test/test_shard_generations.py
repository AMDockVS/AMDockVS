"""Generations: a rewrite of the sharded library switches atomically and forgets its grandparent.

The three things worth pinning down: a scope never spans two generations, a switch is all or
nothing, and the window is two deep — anything older goes, files included.
"""
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.io.shards import ShardGenerationWriter
from amdockvs.models.screening import ScreeningShard
from amdockvs.molecules.storage import (
    ShardStore,
    activate_generation,
    active_generation_id,
    create_generation,
    shard_scope_spec,
)
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.core.vocab import ShardState


@pytest.fixture
def project(tmp_path):
    runtime = AMDockVSRuntime()
    runtime.create_project(name="generations", folder=tmp_path)
    yield runtime.molsuite.project_db, tmp_path
    runtime.close_project()


def _populate(project_db, generation_id, directory, *, count=3, tag="g"):
    """Write `count` shard files and their inventory rows into one generation."""
    paths = []
    with project_db.get_session() as session:
        for index in range(count):
            path = directory / f"{tag}_{index:05d}.mshard"
            path.write_bytes(b"shard")
            paths.append(path)
            session.add(
                ScreeningShard(
                    generation_id=generation_id,
                    source="lib.smi",
                    shard_index=index,
                    path=str(path),
                    input_format="smi",
                    n_records=1000,
                    state=ShardState.PENDING,
                )
            )
        session.commit()
    return paths


def test_a_scope_never_spans_two_generations(project, tmp_path):
    project_db, root = project
    first = create_generation(project_db, step="import", shard_dir=root)
    activate_generation(project_db, first)
    _populate(project_db, first, tmp_path, count=3, tag="first")

    second = create_generation(project_db, step="standardize", shard_dir=root)
    _populate(project_db, second, tmp_path, count=2, tag="second")

    store = ShardStore(project_db)
    whole = shard_scope_spec(state=None)
    # The new generation exists in the table but is not the library yet.
    assert store.count(whole) == 3
    assert active_generation_id(project_db) == first

    activate_generation(project_db, second)
    assert store.count(whole) == 2
    assert {Path(row["path"]).name.split("_")[0] for row in store.iter_rows(whole)} == {"second"}


def test_a_failed_rewrite_leaves_the_library_alone(project, tmp_path):
    """The switch is one flag at the end, so a job that dies mid-flight changes nothing."""
    project_db, root = project
    first = create_generation(project_db, step="import", shard_dir=root)
    activate_generation(project_db, first)
    _populate(project_db, first, tmp_path, count=3, tag="first")

    second = create_generation(project_db, step="protonate", shard_dir=root)
    writer = ShardGenerationWriter(project_db=project_db, generation_id=second)
    writer.handle("chunk-0", [{
        "source": "lib.smi", "shard_index": 0, "path": str(tmp_path / "p_00000.mshard"),
        "input_format": "smi", "n_records": 998, "state": ShardState.READY,
    }])
    writer.on_error("chunk-1", "boom")
    # flush() never runs: the job failed.

    store = ShardStore(project_db)
    assert store.count(shard_scope_spec(state=None)) == 3
    assert store.record_count(shard_scope_spec(state=None)) == 3000

    writer.flush()
    assert store.count(shard_scope_spec(state=None)) == 1
    assert store.record_count(shard_scope_spec(state=None)) == 998


def test_the_window_is_two_deep(project, tmp_path):
    project_db, root = project
    grandparent = create_generation(project_db, step="import", shard_dir=root)
    activate_generation(project_db, grandparent)
    doomed = _populate(project_db, grandparent, tmp_path, count=2, tag="gp")

    parent = create_generation(project_db, step="standardize", shard_dir=root)
    kept = _populate(project_db, parent, tmp_path, count=2, tag="p")
    activate_generation(project_db, parent)

    child = create_generation(project_db, step="generate_3d", shard_dir=root)
    _populate(project_db, child, tmp_path, count=2, tag="c")
    activate_generation(project_db, child)

    assert all(not path.exists() for path in doomed), "the grandparent's files must be swept"
    assert all(path.exists() for path in kept), "the parent is what a redo reads"
    with project_db.get_session() as session:
        from sqlmodel import select

        rows = session.exec(select(ScreeningShard)).all()
    assert {int(row.generation_id) for row in rows} == {parent, child}
