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


@pytest.fixture
def project(tmp_path):
    runtime = AMDockVSRuntime()
    runtime.create_project(name="generations", folder=tmp_path)
    yield runtime.workspace.project_db, tmp_path
    runtime.close_project()


def _populate(project_db, generation_id, directory, *, count=3, tag="g"):
    """Write `count` shard files and their inventory rows into one generation."""
    paths = []
    with project_db.get_session() as session:
        for index in range(count):
            path = directory / f"{tag}_{index:05d}.parquet"
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
    whole = shard_scope_spec()
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
        "source": "lib.smi", "shard_index": 0, "path": str(tmp_path / "p_00000.parquet"),
        "input_format": "smi", "n_records": 998,
    }])
    writer.on_error("chunk-1", "boom")
    # flush() never runs: the job failed.

    store = ShardStore(project_db)
    assert store.count(shard_scope_spec()) == 3
    assert store.record_count(shard_scope_spec()) == 3000

    writer.flush()
    assert store.count(shard_scope_spec()) == 1
    assert store.record_count(shard_scope_spec()) == 998


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


def test_a_rewrite_counts_its_ligands_against_the_library_it_replaces(project, tmp_path):
    """What the monitor shows of a shard step: ligands written so far, of the library's."""
    from amdockvs.molecules.storage import shard_generation_progress

    project_db, root = project
    first = create_generation(project_db, step="import", shard_dir=root)
    activate_generation(project_db, first)
    _populate(project_db, first, tmp_path, count=3, tag="first")
    second = create_generation(project_db, step="protonate", shard_dir=root)
    assert shard_generation_progress(project_db, second) == (0, 3000)
    _populate(project_db, second, tmp_path, count=2, tag="second")
    assert shard_generation_progress(project_db, second) == (2000, 3000)
    activate_generation(project_db, second)  # switched: the new library is all of it
    assert shard_generation_progress(project_db, second) == (2000, 2000)


def test_the_monitor_measures_only_the_jobs_the_runtime_tracks(tmp_path):
    runtime = AMDockVSRuntime()
    runtime.track_job_items("job-1", lambda: (5, 9), unit="ligands")
    assert runtime.measure_job("job-1") == {"done": 5, "total": 9, "unit": "ligands"}
    assert runtime.measure_job("other") is None


def test_chunk_items_count_full_chunks_and_a_short_last_one(monkeypatch):
    """Docking: 23 pairs in chunks of 10 read 10, 20, then 23 as chunks end, off the engine alone."""
    from types import SimpleNamespace

    done = [1]
    engine = SimpleNamespace(status=lambda engine_id: SimpleNamespace(chunks_done=done[0]))
    runtime = AMDockVSRuntime()
    monkeypatch.setattr(runtime, "workspace", SimpleNamespace(engine=engine))
    runtime._engine_ids()["job-1"] = 7
    runtime.track_chunk_items("job-1", 23, 10, unit="ligands")
    assert runtime.measure_job("job-1") == {"done": 10, "total": 23, "unit": "ligands"}
    done[0] = 3
    assert runtime.measure_job("job-1")["done"] == 23


def test_the_project_summary_reads_the_library_off_the_inventory(project, tmp_path):
    """A sharded library is not rows: its stats come from the active generation's shards."""
    from amdockvs.models import DockingRun
    from amdockvs.models.screening import ScreeningTarget, ShardPrep
    from amdockvs.molecules.library_stats import format_bytes, library_stats

    project_db, root = project
    assert library_stats(project_db).shards == 0  # no library yet
    first = create_generation(project_db, step="import", shard_dir=root)
    activate_generation(project_db, first)
    _populate(project_db, first, tmp_path, count=3, tag="first")
    second = create_generation(project_db, step="protonate", shard_dir=root)
    activate_generation(project_db, second)
    shard_ids = []
    with project_db.get_session() as session:
        for index, error in enumerate(("", "", "boom")):
            row = ScreeningShard(generation_id=second, source="lib.smi", shard_index=index,
                                 path=str(tmp_path / f"p_{index}.parquet"), input_format="smi",
                                 n_records=0 if error else 900, error=error)
            session.add(row)
            session.flush()
            shard_ids.append(row.id)
        session.add(ShardPrep(shard_id=shard_ids[0], family="ad4", n_records=899, n_failed=1))
        session.add(DockingRun(id=1, engine="vina"))
        session.flush()
        session.add(ScreeningTarget(docking_run_id=1, receptor_molecule_id=7, ligands_done=1800, hits=12))
        session.commit()

    stats = library_stats(project_db, tmp_path)
    assert (stats.ligands, stats.shards, stats.failed_shards) == (1800, 3, 1)
    assert (stats.last_step, stats.previous_step) == ("protonate", "import")
    assert stats.prepared == {"ad4": (1, 899, 1)}
    assert (stats.campaigns, stats.receptors_docked, stats.ligands_docked, stats.hits) == (1, 1, 1800, 12)
    assert stats.disk_bytes > 0
    assert format_bytes(1536) == "1.5 KB" and format_bytes(12) == "12 B"
