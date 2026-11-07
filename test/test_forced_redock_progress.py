"""A forced re-dock's Docked bar starts empty: the older results of its pairs stay until it ends."""
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from amdockvs.docking.results.tables import receptor_progress
from amdockvs.models import DockingResultRecord, DockingRun, MoleculeRecord
from amdockvs.screening.campaign import plan_targets


class _DB:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

    @contextmanager
    def get_session(self):
        with Session(self.engine) as session:
            yield session


def test_a_forced_redock_counts_only_what_it_docked():
    db = _DB()
    with db.get_session() as s:
        s.add(MoleculeRecord(id=1, name="rec", is_receptor=True))
        s.add_all([MoleculeRecord(id=i, name=f"l{i}", is_ligand=True) for i in (2, 3, 4)])
        s.add_all([DockingRun(id=1, run_id="old", engine="vina", protocol_hash="h"),
                   DockingRun(id=2, run_id="new", engine="vina", protocol_hash="h")])
        s.add_all([DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=i, docking_run_id=1, pose_rank=1)
                   for i in (2, 3, 4)])
        s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=2, docking_run_id=2, pose_rank=1))
        s.commit()
    plan_targets(db, docking_run_id=2, receptors=[{"receptor_id": 1}], ligands_total=3, engine="vina")
    assert receptor_progress(db, [1], "h")[1]["progress"] == 3  # skipping: what was docked is done
    plan_targets(db, docking_run_id=2, receptors=[{"receptor_id": 1}], ligands_total=3, engine="vina", forced=True)
    assert receptor_progress(db, [1], "h")[1]["progress"] == 1
