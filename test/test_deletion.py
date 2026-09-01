"""Cascade row deletion: deleting a molecule also removes its docking results and complexes;
deleting a complex removes its results but keeps the molecules."""
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine, select

from amdockvs.deletion import delete_complexes, delete_molecules
from amdockvs.models import ComplexRecord, DockingResultRecord, MoleculeRecord


class _FakeDB:
    def __init__(self, engine):
        self.engine = engine

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


if __name__ == "__main__":
    test_delete_molecule_cascades_results_and_complexes()
    test_delete_complex_keeps_molecules()
    print("OK")
