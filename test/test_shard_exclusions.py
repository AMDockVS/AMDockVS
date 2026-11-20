"""Exclusions over a sharded library: a cut narrows every step that selects, none that transforms."""
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
    rt.create_project(name="exclusions", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _import(runtime, tmp_path, n=10, shard_size=4):
    source = tmp_path / "lib.smi"
    source.write_text("".join(f"{'C' * (i + 1)}O mol{i}\n" for i in range(n)))
    jobs = runtime.loader.shard_ligands([str(source)], executor_name="thread", shard_size=shard_size)
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states


def test_a_cut_narrows_what_the_steps_read_but_not_what_they_transform(runtime, tmp_path):
    from ms_flow.core.data.exclusions import list_exclusions, set_exclusion_active

    from amdockvs.io.shards import shard_key
    from ms_flow.core.data import catalog
    from amdockvs.molecules.storage import ShardStore

    _import(runtime, tmp_path)
    project_db = runtime.workspace.project_db
    store = ShardStore(project_db)
    rows = sorted(catalog.shards(project_db, whole=True), key=lambda row: row["base_id"])
    batch = int(rows[0]["scope"])
    assert (store.count(), store.record_count()) == (3, 10)

    # The whole first shard (ids 0-3) and one record of the second.
    cut = store.exclude([shard_key(batch, i) for i in (0, 1, 2, 3, 5)], reason="filter")
    assert cut is not None
    narrowed = list(store.iter_rows())
    assert [row["shard_index"] for row in narrowed] == [1, 2]
    assert narrowed[0]["record_ids"] == [4, 6, 7] and narrowed[0]["n_records"] == 3
    assert "record_ids" not in narrowed[1]
    assert (store.count(), store.record_count()) == (2, 5)
    # A transform still sees every shard: preparing the library once survives an undo.
    assert store.count(whole=True) == 3

    assert [(c["id"], c["active"], c["reason"], c["n"]) for c in list_exclusions(project_db)] == [
        (cut, True, "filter", 5)]
    set_exclusion_active(project_db, cut, False)
    assert (store.count(), store.record_count()) == (3, 10)


def test_a_narrowed_shard_prepares_whole_and_docks_only_what_is_left(runtime, tmp_path):
    from ms_flow.core.data.shard import Shard

    from amdockvs.io.shards import shard_ids, shard_key
    from ms_flow.core.data import catalog
    from amdockvs.molecules.storage import ShardStore, iter_prepared_shards, prepared_shard_path

    _import(runtime, tmp_path)
    project_db = runtime.workspace.project_db
    rows = sorted(catalog.shards(project_db, whole=True), key=lambda row: row["base_id"])
    batch, first = int(rows[0]["scope"]), rows[0]
    ids = shard_ids(first)
    ShardStore(project_db).exclude([shard_key(batch, 1)], reason="filter")

    job = runtime.docking.prepare_ligand_shards(executor_name="thread")
    state = runtime.wait_for_jobs([job], timeout_s=180)[job]
    assert str(state.status).endswith("completed"), state
    with Shard.open(prepared_shard_path(first["path"], "ad4", ids)) as shard:
        assert [record.id for record in shard] == [0, 1, 2, 3]  # prepared whole

    prepared = {int(row["id"]): row for row in iter_prepared_shards(project_db, engine="ad4")}
    row = prepared[int(first["id"])]
    assert row["record_ids"] == [0, 2, 3] and row["n_records"] == 3
    # And that is what a docking of it would read out of the prepared file.
    with Shard.open(row["prepared_path"], record_ids=row["record_ids"]) as shard:
        assert [record.id for record in shard] == [0, 2, 3]
