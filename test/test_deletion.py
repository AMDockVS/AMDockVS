"""Cascade row deletion: deleting a molecule also removes its docking results and complexes;
deleting a complex removes its results but keeps the molecules."""
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine, select

from amdockvs.project.deletion import clear_derived, delete_complexes, delete_molecules
from amdockvs.models import (
    ComplexRecord,
    DockingResultRecord,
    FingerprintRecord,
    LigandActivity,
    MoleculeModel,
    MoleculeRecord,
)


class _FakeDB:
    def __init__(self, engine, db_path=None):
        self.engine = engine
        self.db_path = db_path

    @contextmanager
    def get_session(self):
        with Session(self.engine) as session:
            yield session


def _seed():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(MoleculeRecord(id=1, name="rec", is_receptor=True))
        s.add(MoleculeRecord(id=2, name="lig", is_ligand=True))
        s.add(ComplexRecord(id=1, receptor_molecule_id=1, ligand_molecule_id=2, purpose="screening"))
        for rank in (1, 2):
            s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=2, engine="vina", pose_rank=rank))
        s.commit()
    return _FakeDB(engine)


def _counts(db):
    with db.get_session() as s:
        return (
            len(s.exec(select(MoleculeRecord)).all()),
            len(s.exec(select(ComplexRecord)).all()),
            len(s.exec(select(DockingResultRecord)).all()),
        )


def test_delete_molecule_cascades_results_and_complexes():
    db = _seed()
    deleted = delete_molecules(db, [2])  # delete the ligand
    assert deleted == 1
    mols, complexes, results = _counts(db)
    assert mols == 1            # only the receptor remains
    assert complexes == 0       # the pair referencing the ligand is gone
    assert results == 0         # both poses gone


def test_delete_complex_keeps_molecules():
    db = _seed()
    deleted = delete_complexes(db, [1])
    assert deleted == 1
    mols, complexes, results = _counts(db)
    assert mols == 2            # molecules untouched
    assert complexes == 0
    assert results == 0         # the pair's docking results removed


def _project(tmp_path):
    """Two ligands: 2 owns a.sdf, a model and a fingerprint; 3 shares b.sdf as its stored file."""
    engine = create_engine(f"sqlite:///{tmp_path / 'project.db'}")
    SQLModel.metadata.create_all(engine)
    for name in ("a.sdf", "a_edit.sdf", "model.sdf", "b.sdf", "b.mshard"):
        (tmp_path / name).write_text("x")
    outside = tmp_path.parent / f"{tmp_path.name}_outside.sdf"
    outside.write_text("x")
    with Session(engine) as s:
        s.add(MoleculeRecord(id=2, name="a", is_ligand=True, stored_path="a.sdf", current_path="a_edit.sdf"))
        s.add(MoleculeRecord(id=3, name="b", is_ligand=True, stored_path="b.sdf", current_path=str(outside)))
        s.add(MoleculeRecord(id=4, name="c", is_ligand=True, stored_path="b.mshard", current_path="b.sdf"))
        s.add(MoleculeModel(molecule_id=2, model_index=1, file_path="model.sdf"))
        s.add(FingerprintRecord(molecule_id=2, fp_type="morgan", fp_binary=b""))
        s.add(LigandActivity(molecule_id=2, activity_type="IC50", value=1.0))
        s.commit()
    return _FakeDB(engine, tmp_path / "project.db"), outside


def test_clear_derived_keeps_the_compound_and_its_structure(tmp_path):
    db, _ = _project(tmp_path)
    clear_derived(db, [2])
    with db.get_session() as s:
        assert s.exec(select(MoleculeModel)).all() == []
        assert s.exec(select(FingerprintRecord)).all() == []
        assert len(s.exec(select(LigandActivity)).all()) == 1   # describes the compound: stays
    assert not (tmp_path / "model.sdf").exists()
    assert (tmp_path / "a.sdf").exists() and (tmp_path / "a_edit.sdf").exists()


def test_delete_molecule_removes_only_its_own_files(tmp_path):
    db, outside = _project(tmp_path)
    assert delete_molecules(db, [2, 3]) == 2
    with db.get_session() as s:
        assert s.exec(select(LigandActivity)).all() == []
    assert not any((tmp_path / n).exists() for n in ("a.sdf", "a_edit.sdf", "model.sdf"))
    assert (tmp_path / "b.sdf").exists()      # molecule 4 still names it
    assert outside.exists()                   # not the project's
    delete_molecules(db, [4])
    assert not (tmp_path / "b.sdf").exists()
    assert (tmp_path / "b.mshard").exists()   # a container holds other molecules


def test_deleting_a_ligand_row_takes_its_run_poses_only():
    from types import SimpleNamespace

    from amdockvs.docking.api import DockingAPI

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for ligand, run, rank in ((2, "r1", 1), (2, "r1", 2), (2, "r2", 1), (3, "r1", 1)):
            s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=ligand, engine="vina",
                                      pose_rank=rank, metrics={"run_id": run, "protocol": {"hash": "h"}}))
        s.commit()
    api = DockingAPI.__new__(DockingAPI)
    api.runtime = SimpleNamespace(_require_active_project=lambda: None,
                                  workspace=SimpleNamespace(project_db=_FakeDB(engine)))
    assert api.delete_results([1], with_siblings=True) == 2
    with Session(engine) as s:
        left = {(r.ligand_molecule_id, r.metrics["run_id"]) for r in s.exec(select(DockingResultRecord))}
    assert left == {(2, "r2"), (3, "r1")}


if __name__ == "__main__":
    test_delete_molecule_cascades_results_and_complexes()
    test_delete_complex_keeps_molecules()
    print("OK")
