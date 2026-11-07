"""A forced re-dock drops its pairs' older results when it starts, and Docked speaks of the selection."""
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine, select

from amdockvs.docking.repository import molecule_scope_spec
from amdockvs.docking.results.repository import drop_redocked_pairs
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


def test_a_forced_redock_drops_the_older_results_of_its_pairs_only():
    db = _DB()
    with db.get_session() as s:
        s.add(MoleculeRecord(id=1, name="rec", is_receptor=True))
        s.add_all([MoleculeRecord(id=i, name=f"l{i}", is_ligand=True) for i in (2, 3, 4)])
        s.add_all([DockingRun(id=1, run_id="old", engine="vina", run_kind="screening", protocol_hash="h"),
                   DockingRun(id=2, run_id="new", engine="vina", run_kind="screening", protocol_hash="h"),
                   DockingRun(id=3, run_id="other", engine="vina", run_kind="screening", protocol_hash="x")])
        s.add_all([DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=i, docking_run_id=1, pose_rank=1)
                   for i in (2, 3, 4)])
        s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=2, docking_run_id=2, pose_rank=1))
        s.add(DockingResultRecord(receptor_molecule_id=1, ligand_molecule_id=3, docking_run_id=3, pose_rank=1))
        s.commit()
    # Skipping ligands 2 and 3: both were docked before, and the run has 2 of 3 as its selection.
    plan_targets(db, docking_run_id=2, receptors=[{"receptor_id": 1, "ligands_done": 2}],
                 ligands_total=2, engine="vina")
    assert receptor_progress(db, [1], "h")[1]["ligands"] == 2
    assert receptor_progress(db, [1], "h")[1]["progress"] == 3  # the 2 skipped + the 1 it recorded

    # Ligands 2 and 3 are re-docked; 4 is outside the selection, run 3 is another program.
    scope = molecule_scope_spec(entity_kind="ligand", engine="ad4", extra_filters={"id__in": [2, 3]}, order=())
    assert drop_redocked_pairs(db, docking_run_id=2, receptor_ids=[1], ligand_scope=scope,
                               engine="vina", protocol_hash="h") == 2
    with db.get_session() as s:
        left = {(r.docking_run_id, r.ligand_molecule_id) for r in s.exec(select(DockingResultRecord))}
    assert left == {(1, 4), (2, 2), (3, 3)}

    # Forced: nothing of its pairs is left to count, so Docked is what this run has recorded.
    plan_targets(db, docking_run_id=2, receptors=[{"receptor_id": 1, "ligands_done": 0}],
                 ligands_total=2, engine="vina")
    assert receptor_progress(db, [1], "h")[1]["progress"] == 1
