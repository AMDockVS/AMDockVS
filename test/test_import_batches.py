"""One `import_batches` row per imported file: counted when the job ends, and a file imported
completely before is refused unless forced."""
import pytest
from sqlmodel import delete, select

from amdockvs.io.batches import DuplicateImportError, duplicate_imports, library_names
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
    assert duplicate_imports(project_db, [library]) == []  # the hash changed too
    runtime.wait_for_jobs(runtime.loader.load_ligands([library]), timeout_s=300)


def test_shards_count_into_their_batch(runtime, tmp_path):
    library = tmp_path / "big.smi"
    library.write_text("".join(f"{'C' * (i % 5 + 1)}O m{i}\n" for i in range(7)))
    runtime.wait_for_jobs(runtime.loader.shard_ligands([library], shard_size=3), timeout_s=300)
    with runtime.workspace.project_db.get_session() as session:
        (batch,) = session.exec(select(ImportBatch)).all()
    assert (batch.kind, batch.n_records, batch.n_imported) == ("shards", 7, 7)


def test_deleted_molecules_can_be_imported_again(runtime, tmp_path):
    library = tmp_path / "deleted.smi"
    library.write_text("CCO a\nCCN b\nCCC c\n")
    runtime.wait_for_jobs(runtime.loader.load_ligands([library]), timeout_s=300)
    project_db = runtime.workspace.project_db
    with project_db.get_session() as session:
        molecules = session.exec(select(MoleculeRecord)).all()
        batch = session.exec(select(ImportBatch)).one()

    # A partial deletion still leaves molecules that a reimport would duplicate.
    runtime.molecules.delete([molecules[0].id])
    assert runtime.loader.duplicate_imports([library]) == [str(library.resolve())]
    runtime.molecules.delete([m.id for m in molecules[1:]])
    assert runtime.loader.duplicate_imports([library]) == []

    # Preserve provenance, but do not mistake its counters for live molecules.
    with project_db.get_session() as session:
        historical = session.get(ImportBatch, batch.id)
        assert (historical.n_records, historical.n_imported) == (3, 3)
    runtime.wait_for_jobs(runtime.loader.load_ligands([library]), timeout_s=300)
    with project_db.get_session() as session:
        assert len(session.exec(select(MoleculeRecord)).all()) == 3
        assert len(session.exec(select(ImportBatch)).all()) == 2
    assert runtime.loader.duplicate_imports([library]) == [str(library.resolve())]


def test_deleted_shards_are_not_duplicate_imports(runtime, tmp_path):
    from amdockvs.models.screening import ScreeningShard

    library = tmp_path / "deleted-shards.smi"
    library.write_text("CCO a\nCCN b\nCCC c\n")
    runtime.wait_for_jobs(runtime.loader.shard_ligands([library], shard_size=2), timeout_s=300)
    assert runtime.loader.duplicate_imports([library]) == [str(library.resolve())]
    with runtime.workspace.project_db.get_session() as session:
        session.exec(delete(ScreeningShard))
        session.commit()
    assert runtime.loader.duplicate_imports([library]) == []


def test_reference_ligands_do_not_count_as_existing_receptors(runtime, tmp_path):
    from pathlib import Path

    source = tmp_path / "receptor.pdb"
    ligand = (Path(__file__).parent / "data" / "ccd_ligand.pdb").read_text().splitlines()
    atoms = [
        f"ATOM  {i:5d}  CA  ALA A{i:4d}    {i * 3.8:8.3f}{0:8.3f}{0:8.3f}  1.00 20.00           C"
        for i in range(1, 6)
    ]
    source.write_text("\n".join(atoms + [line for line in ligand if line.startswith("HETATM")] + ["END", ""]))
    runtime.wait_for_jobs(runtime.loader.load_receptors([source], build_specs=False), timeout_s=300)
    project_db = runtime.workspace.project_db
    with project_db.get_session() as session:
        molecules = session.exec(select(MoleculeRecord)).all()
        receptor = next(m for m in molecules if m.is_receptor)
        reference = next(m for m in molecules if m.usage_class == "reference")
    assert runtime.loader.duplicate_imports([source]) == [str(source.resolve())]

    # Simulate a project where an earlier deletion left only its reference ligand.
    with project_db.get_session() as session:
        session.add(MoleculeRecord(
            name="orphaned reference", is_ligand=True, usage_class="reference",
            import_batch_id=reference.import_batch_id,
        ))
        session.commit()
    assert runtime.molecules.delete([receptor.id]) == 1
    assert runtime.loader.duplicate_imports([source]) == []
    with project_db.get_session() as session:
        assert session.get(MoleculeRecord, reference.id) is None
        assert len(session.exec(select(MoleculeRecord)).all()) == 1
    runtime.wait_for_jobs(runtime.loader.load_receptors([source], build_specs=False), timeout_s=300)
    with project_db.get_session() as session:
        assert len(session.exec(select(MoleculeRecord).where(MoleculeRecord.is_receptor)).all()) == 1
