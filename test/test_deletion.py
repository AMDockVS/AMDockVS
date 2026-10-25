"""Cascade row deletion: deleting a molecule also removes its docking results and complexes;
deleting a complex removes its results but keeps the molecules."""
from contextlib import contextmanager

from ms_flow.core.database.project import ProjectStore
from payload_seed import seed_payload

from sqlmodel import Session, SQLModel, create_engine, select

from amdockvs.core.payloads import PAYLOAD_CACHE
from amdockvs.project.deletion import clear_derived, delete_complexes, delete_molecules
from amdockvs.models import (
    ComplexRecord,
    DockingResultRecord,
    DockingRun,
    EngineState,
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
        s.add(ComplexRecord(id=1, receptor_molecule_id=1, ligand_molecule_id=2, purpose="reference"))
        s.add(DockingRun(id=1, engine="vina"))
        for rank in (1, 2):
            s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=2, docking_run_id=1, pose_rank=rank))
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
    """Two ligands: 2 owns a stored and an edited structure, a model, a preparation and a fingerprint;
    3 and 4 share one structure; a cached copy of every payload was opened once."""
    store = ProjectStore.open_at(tmp_path / "project.db")
    ids = {name: seed_payload(store, f"{name}\n", "sdf") for name in
           ("a", "a_edit", "model", "pae", "prepared", "shared", "alone", "frozen")}
    with store.get_session() as s:
        s.add(MoleculeRecord(id=2, name="a", is_ligand=True, stored_payload_id=ids["a"], current_payload_id=ids["a_edit"]))
        s.add(MoleculeRecord(id=3, name="b", is_ligand=True, stored_payload_id=ids["shared"], current_payload_id=ids["alone"]))
        s.add(MoleculeRecord(id=4, name="c", is_ligand=True, stored_payload_id=ids["shared"], current_payload_id=ids["shared"]))
        s.commit()
        s.add(MoleculeModel(molecule_id=2, model_index=1, payload_id=ids["model"], pae_payload_id=ids["pae"]))
        s.add(EngineState(molecule_id=2, role_type="ligand", engine="vina", prepared_payload_id=ids["prepared"]))
        s.add(FingerprintRecord(molecule_id=2, fp_type="morgan", fp_binary=b""))
        s.add(LigandActivity(molecule_id=2, activity_type="IC50", value=1.0))
        s.add(ComplexRecord(receptor_molecule_id=3, ligand_molecule_id=2, purpose="redocking",
                            reference_ligand_payload_id=ids["frozen"]))
        s.commit()
    cache = tmp_path / PAYLOAD_CACHE
    cache.mkdir(parents=True)
    for pid in ids.values():
        (cache / f"{pid}.sdf").write_text("x")
    stored = lambda: {name for name, pid in ids.items() if pid in store.payloads.kinds(ids.values())}
    cached = lambda: {name for name, pid in ids.items() if (cache / f"{pid}.sdf").exists()}
    return store, stored, cached


def test_clear_derived_keeps_the_compound_and_its_structure(tmp_path):
    db, stored, cached = _project(tmp_path)
    clear_derived(db, [2])
    with db.get_session() as s:
        assert s.exec(select(MoleculeModel)).all() == []
        assert s.exec(select(FingerprintRecord)).all() == []
        assert s.exec(select(EngineState)).all() == []
        assert len(s.exec(select(LigandActivity)).all()) == 1   # describes the compound: stays
    for kept in (stored(), cached()):
        assert not {"model", "pae", "prepared"} & kept          # what was computed from it went
        assert {"a", "a_edit", "shared", "alone", "frozen"} <= kept  # the structure did not


def test_delete_molecule_releases_only_its_own_payloads(tmp_path):
    db, stored, cached = _project(tmp_path)
    assert delete_molecules(db, [2, 3]) == 2
    with db.get_session() as s:
        assert s.exec(select(LigandActivity)).all() == []
        assert s.exec(select(ComplexRecord)).all() == []
    assert stored() == cached() == {"shared"}   # molecule 4 still holds it
    delete_molecules(db, [4])
    assert stored() == cached() == set()
    assert db.payloads.check().clean


def test_a_payload_a_result_still_holds_survives_the_molecule(tmp_path):
    db, stored, _ = _project(tmp_path)
    with db.get_session() as s:
        pose = s.get(MoleculeRecord, 3).current_payload_id
        s.add(DockingRun(id=1, engine="vina"))
        s.flush()
        s.add(DockingResultRecord(receptor_molecule_id=4, ligand_molecule_id=4, docking_run_id=1, pose_payload_id=pose))
        s.commit()
    delete_molecules(db, [3])
    assert "alone" in stored()   # the other molecule's result still shows it


def test_deleting_a_ligand_row_takes_its_run_poses_only():
    from types import SimpleNamespace

    from amdockvs.docking.api import DockingAPI

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([DockingRun(id=1, run_id="r1", engine="vina", protocol_hash="h"),
                   DockingRun(id=2, run_id="r2", engine="vina", protocol_hash="h")])
        for ligand, run, rank in ((2, 1, 1), (2, 1, 2), (2, 2, 1), (3, 1, 1)):
            s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=ligand, docking_run_id=run,
                                      pose_rank=rank))
        s.commit()
    api = DockingAPI.__new__(DockingAPI)
    api.runtime = SimpleNamespace(_require_active_project=lambda: None,
                                  workspace=SimpleNamespace(project_db=_FakeDB(engine)))
    assert api.delete_results([1], with_siblings=True) == 2
    with Session(engine) as s:
        left = {(r.ligand_molecule_id, r.docking_run_id) for r in s.exec(select(DockingResultRecord))}
    assert left == {(2, 2), (3, 1)}


if __name__ == "__main__":
    test_delete_molecule_cascades_results_and_complexes()
    test_delete_complex_keeps_molecules()
    print("OK")
