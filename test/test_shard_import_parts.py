"""M7: a worker-written import. Each span is filtered, encoded and written by a worker as a part
with planned ids (source ordinal * F); the parent only commits the parts."""
from pathlib import Path

import pytest
from sqlmodel import select

from ms_flow.core.data import catalog
from ms_flow.core.data.shard import Shard, read_dropped

import amdockvs.io.jobs as jobs
from amdockvs.core.constants import RESOURCE_SHARDS
from amdockvs.io.batches import register_batches
from amdockvs.io.shards import shard_import_writer, shard_number, write_ligand_span
from amdockvs.models import ImportBatch
from amdockvs.runtime import AMDockVSRuntime

SMILES = ["CCO", "CCN", "CCC", "CCCl", "CCBr", "c1ccccc1", "CC(=O)O", "CCCCO", "OCCO", "NCCN"]


@pytest.fixture
def make_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    made = []

    def make(name="p"):
        rt = AMDockVSRuntime()
        rt.create_project(name=name, folder=tmp_path / name)
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close_project()


def _library(folder: Path, name: str, smiles) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("".join(f"{s} m{i}\n" for i, s in enumerate(smiles)))
    return path


def _job(rt, files, *, run="r1", shard_size=4, prefilter=None):
    """(params, config, chunks) of the import, as the job would plan them."""
    db = rt.workspace.project_db
    gen = catalog.active_generation_id(db)  # an import writes into the active one, as the API does
    if gen is None:
        gen = catalog.create_generation(db, step="import", folder=rt.get_project_resource_path(RESOURCE_SHARDS))
        catalog.activate_generation(db, gen)
    params = {"file_paths": [str(f) for f in files], "batch_size": shard_size, "generation_id": gen, "run": run,
              "import_batch_ids": register_batches(db, files, kind="shards")}
    if prefilter:
        params["prefilter"] = prefilter
    config = {"project_db": db, "project_resources": rt.workspace.get_project_resource_map()}
    return params, config, list(jobs.shard_ligand_chunks(params, config))


def _commit(params, config, results, *, finish=True):
    writer = shard_import_writer(params, config)
    for result in results:
        writer.handle("chunk", result)
    writer.flush() if finish else writer.close()


def _records(db):
    """(batch, id, record, source ordinal) of every live record, and the shard ranges."""
    records, ranges = [], []
    for row in catalog.shards(db, catalog.shard_spec(), whole=True):
        ranges.append((row["scope"], row["base_id"], row["slot_count"]))
        with Shard.open(row["path"], shard=shard_number(row)) as shard:
            records += [(row["scope"], record.id, record.load()) for record in shard]
    return sorted(records), sorted(ranges)


def _imported(db):
    with db.get_session() as session:
        return sorted(int(b.n_imported) for b in session.exec(select(ImportBatch)).all())


def test_ids_do_not_depend_on_completion_order(make_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "SHARD_IMPORT_SPAN_RECORDS", 3)
    files = [_library(tmp_path / "src", "a.smi", SMILES), _library(tmp_path / "src", "b.smi", SMILES[:7])]
    seen = []
    for name, order in (("forward", 1), ("backward", -1)):
        rt = make_runtime(name)
        params, config, chunks = _job(rt, files)
        assert len(chunks) == 4 + 3
        _commit(params, config, [write_ligand_span(chunk) for chunk in chunks[::order]])
        records, ranges = _records(rt.workspace.project_db)
        # the batches are numbered alike in both projects: compare by file
        seen.append(([(r[1], r[2]) for r in records], ranges))
        assert sorted(r[1] for r in records if r[0] == records[0][0]) == list(range(10))  # id = ordinal (F = 1)
    assert seen[0] == seen[1]


def test_a_killed_import_resumes_without_duplicates(make_runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "SHARD_IMPORT_SPAN_RECORDS", 3)
    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES)])
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks[:2]], finish=False)  # killed
    assert len(_records(db)[0]) == 6
    # the resumed job runs every task again (the in-flight ones, and some done ones): same part ids
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    records, _ranges = _records(db)
    assert [r[1] for r in records] == list(range(10))
    assert _imported(db) == [10]
    shard_dir = Path(rt.get_project_resource_path(RESOURCE_SHARDS))
    assert not [p for p in shard_dir.iterdir() if p.name.startswith(".part-")]  # no part is left behind


def test_fan_out_over_the_limit_is_a_listed_drop(make_runtime, tmp_path):
    many = ".".join(f"c1ccccc1{'C' * i}" for i in range(14))  # more than 8 molecules from one record
    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", ["CCO", many, "CCN.c1ccccc1O"])],
                                  prefilter={"split_fragments": True})
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    records, _ranges = _records(db)
    ids = [r[1] for r in records]
    assert ids[0] == 0 and not [i for i in ids if 8 <= i < 16] and all(16 <= i < 24 for i in ids[1:])  # ordinal * 8 + k
    (row,) = catalog.shards(db, catalog.shard_spec(), whole=True)
    ((dropped_id, reason),) = read_dropped(row["path"], shard_number(row))
    assert dropped_id == 8 and "at most 8" in reason
    assert _imported(db) == [len(ids)]  # live records: the dropped one is not counted


def test_imported_counts_are_idempotent(make_runtime, tmp_path):
    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES)])
    results = [write_ligand_span(chunk) for chunk in chunks]
    _commit(params, config, results)
    _commit(params, config, results)  # the same receipts again: committed before, counted once
    assert _imported(db) == [10]


def test_the_parent_sees_no_record(make_runtime, tmp_path, monkeypatch):
    import ms_flow.core.data.container as container

    rt = make_runtime()
    db = rt.workspace.project_db
    encode = container.encode

    def refused(*_a, **_k):
        raise AssertionError("the parent encoded a shard")

    monkeypatch.setattr(container, "encode", refused)
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES)])
    monkeypatch.setattr(container, "encode", encode)
    results = [write_ligand_span(chunk) for chunk in chunks]  # the worker
    for result in results:
        for receipt in result:
            assert "records" not in receipt and not any(isinstance(v, bytes) for v in receipt.values()), receipt
    monkeypatch.setattr(container, "encode", refused)
    _commit(params, config, results)
    assert len(_records(db)[0]) == 10


def test_a_source_over_the_id_bits_is_refused(make_runtime, tmp_path):
    rt = make_runtime()
    _params, _config, (chunk,) = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES)],
                                      prefilter={"split_fragments": True})
    chunk = {**chunk, "span_first_index": (1 << 32) // 8}
    with pytest.raises(ValueError, match=str((1 << 32) // 8)):
        write_ligand_span(chunk)


def test_a_filtered_record_leaves_a_hole_and_its_ordinal(make_runtime, tmp_path):
    from ms_flow.core.data import container

    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", ["CCO", "CCCCCCCCCC", "CCN"])],
                                  prefilter={"max_heavy_atoms": 5})
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    assert [r[1] for r in _records(db)[0]] == [0, 2]  # planned ids: the dropped one is a hole
    (row,) = catalog.shards(db, catalog.shard_spec(), whole=True)
    info, rows = container.read_index(row["path"])
    assert container.read_table(row["path"], info, rows, ["source_index"]).column(0).to_pylist() == [0, 2]


def test_two_sources_share_the_container_not_a_shard(make_runtime, tmp_path):
    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES[:3]),
                                       _library(tmp_path / "src", "b.smi", SMILES[:3])])
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    rows = list(catalog.shards(db, catalog.shard_spec(), whole=True))
    assert len({row["scope"] for row in rows}) == 2 and len(rows) == 2
    assert len({row["path"] for row in rows}) == 1


def test_a_second_import_does_not_overwrite_the_first(make_runtime, tmp_path):
    rt = make_runtime()
    db = rt.workspace.project_db
    for name, run in (("a.smi", "r1"), ("b.smi", "r2")):
        params, config, chunks = _job(rt, [_library(tmp_path / "src", name, SMILES[:2])], run=run)
        _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    rows = list(catalog.shards(db, catalog.shard_spec(), whole=True))
    assert [row["base_id"] for row in rows] == [0, 0] and len({row["scope"] for row in rows}) == 2
    assert len(_records(db)[0]) == 4


def test_a_rewrite_keeps_the_scope_of_its_source(make_runtime, tmp_path):
    """Planned ids start at 0 in every source: a shard is named by (scope, id), so a rewrite
    (the chemistry pipeline) that dropped the scope would make two sources' shards collide."""
    from ms_flow.core.data.shard import file_groups

    from amdockvs.chemistry.tasks import shard_chemistry_task

    rt = make_runtime()
    db = rt.workspace.project_db
    params, config, chunks = _job(rt, [_library(tmp_path / "src", "a.smi", SMILES[:2]),
                                       _library(tmp_path / "src", "b.smi", SMILES[:2])])
    _commit(params, config, [write_ligand_span(chunk) for chunk in chunks])
    for row in catalog.shards(db, catalog.shard_spec(), whole=True):
        (out,) = shard_chemistry_task({"shard_path": row["path"], "shard": shard_number(row), "steps": ["standardize"],
                                       "output_dir": str(tmp_path / "out"), "import_batch_id": row["scope"],
                                       "shard_index": row["shard_index"]})
        assert [g.scope for g in file_groups(out["path"])] == [row["scope"]]
