"""A forced re-dock replaces a result only once it has another one for the same docking."""
from datetime import datetime

from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.docking.results.repository import supersede_results
from amdockvs.models.docking import DockingResult
from amdockvs.models.molecules import MoleculeRecord


def test_only_the_pairs_docked_again_lose_their_older_rows(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        receptor = MoleculeRecord(name="rec", is_receptor=True, stored_path="rec.pdb")
        ligands = [MoleculeRecord(name=f"lig_{i}", is_ligand=True, stored_path=f"lig_{i}.sdf") for i in range(2)]
        session.add_all([receptor, *ligands])
        session.commit()
        again, untouched = ligands[0].id, ligands[1].id

        def result(ligand, run, *, engine="vina", kind="screening", protocol="h", pose_path=""):
            return DockingResult(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand, engine=engine,
                                 pose_path=pose_path or f"{run}.pdbqt", created_at=datetime.now(),
                                 metrics={"run_id": run, "run_kind": kind, "protocol": {"hash": protocol}})

        session.add_all([
            result(again, "old"), result(again, "old"),  # the two poses the new run replaces
            result(untouched, "old"),  # a pair the new run did not get to
            result(again, "old", kind="redocking", pose_path="redocking.pdbqt"),  # another kind of run
            result(again, "old", engine="qvina", pose_path="qvina.pdbqt"),  # another engine
            result(again, "old", protocol="other", pose_path="protocol.pdbqt"),  # another protocol
            result(again, "new"),
        ])
        session.commit()

    assert supersede_results(store, run_id="") == 0
    assert supersede_results(store, run_id="new") == 2
    with store.get_session() as session:
        left = sorted((row.ligand_molecule_id, row.pose_path) for row in session.exec(select(DockingResult)).all())
    assert left == sorted([(again, "new.pdbqt"), (untouched, "old.pdbqt"), (again, "redocking.pdbqt"),
                           (again, "qvina.pdbqt"), (again, "protocol.pdbqt")])
    assert supersede_results(store, run_id="new") == 0  # and once is enough
