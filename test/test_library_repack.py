"""A library repacked after a strong cut (M12b): one job, the same ligands in fuller shards, the
previous generation kept, gone back to, and deleted by hand."""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="repack", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _usable(db):
    from ms_flow.core.data import catalog

    return sorted(i for row in catalog.shards(db) for i in (row.get("record_ids") or
                                                             range(row["base_id"], row["base_id"] + row["slot_count"])))


def test_a_repacked_library_keeps_its_ligands_and_the_old_one_comes_back(runtime, tmp_path):
    from ms_flow.core.data import catalog, exclusions

    source = tmp_path / "lib.smi"
    source.write_text("".join(f"{'C' * (i + 1)}O mol{i}\n" for i in range(12)))
    states = runtime.wait_for_jobs(list(runtime.loader.shard_ligands([str(source)], executor_name="thread",
                                                                     shard_size=4)), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    db = runtime.workspace.project_db
    old = catalog.active_generation_id(db)
    [scope] = {row["scope"] for row in catalog.shards(db)}
    exclusions.exclude(db, {scope: [i for i in range(12) if i % 3]}, reason="filter")
    before = _usable(db)
    assert before == [0, 3, 6, 9] and len(list(catalog.shards(db))) == 3

    assert runtime.loader.repack_offer() is None
    job = runtime.loader.repack_library(executor_name="thread")
    state = runtime.wait_for_jobs([job], timeout_s=60)[job]
    assert str(state.status).endswith("completed"), state
    new = catalog.active_generation_id(db)
    assert new != old and len(list(catalog.shards(db))) == 1 and _usable(db) == before

    listed = {row["id"]: row for row in runtime.loader.library_generations()}
    assert listed[new]["active"] and listed[old]["kept"] and listed[old]["records"] == 12
    runtime.loader.go_back_generation(old)
    assert catalog.active_generation_id(db) == old and len(list(catalog.shards(db))) == 3
    runtime.loader.delete_generation(new)
    assert new not in {row["id"] for row in runtime.loader.library_generations()}
