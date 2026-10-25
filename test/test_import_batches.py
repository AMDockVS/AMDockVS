"""One `import_batches` row per imported file: counted when the job ends, and a file imported
completely before is refused unless forced."""
import pytest
from sqlmodel import select

from amdockvs.io.batches import DuplicateImportError, library_names, previous_import, quick_hash
from amdockvs.models import ImportBatch, MoleculeRecord
from amdockvs.runtime import AMDockVSRuntime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rt = AMDockVSRuntime()
    rt.create_project(name="batches", folder=tmp_path / "project")
    yield rt
    rt.close_project()


def test_a_file_is_counted_and_not_imported_twice(runtime, tmp_path):
    library = tmp_path / "lib.smi"
    library.write_text("CCO a\nCCN b\nCCC c\n")
    project_db = runtime.workspace.project_db
    runtime.wait_for_jobs(runtime.loader.load_ligands([library]), timeout_s=300)

    with project_db.get_session() as session:
        (batch,) = session.exec(select(ImportBatch)).all()
        molecules = session.exec(select(MoleculeRecord)).all()
    assert (batch.n_records, batch.n_imported, batch.format, batch.kind) == (3, 3, "smiles", "rows")
    assert batch.job_id and batch.extra["context"] == "general"
    assert {m.import_batch_id for m in molecules} == {batch.id}
    assert library_names(project_db) == {batch.id: "lib"}

    with pytest.raises(DuplicateImportError):
        runtime.loader.load_ligands([library])
    runtime.wait_for_jobs(runtime.loader.load_ligands([library], force=True), timeout_s=300)
    with project_db.get_session() as session:
        assert len(session.exec(select(MoleculeRecord)).all()) == 6

    # A file that grew since is a partial import, not a duplicate: it goes in without force.
    library.write_text("CCO a\nCCN b\nCCC c\nCCCC d\n")
    assert previous_import(project_db, library, quick_hash(library)) == ""  # the hash changed too
    runtime.wait_for_jobs(runtime.loader.load_ligands([library]), timeout_s=300)


def test_shards_count_into_their_batch(runtime, tmp_path):
    library = tmp_path / "big.smi"
    library.write_text("".join(f"{'C' * (i % 5 + 1)}O m{i}\n" for i in range(7)))
    runtime.wait_for_jobs(runtime.loader.shard_ligands([library], shard_size=3), timeout_s=300)
    with runtime.workspace.project_db.get_session() as session:
        (batch,) = session.exec(select(ImportBatch)).all()
    assert (batch.kind, batch.n_records, batch.n_imported) == ("shards", 7, 7)
