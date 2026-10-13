"""A forced re-dock replaces a result only once it has another one for the same docking, and a result
that goes takes what was computed from it."""
from datetime import datetime

from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.docking.results.repository import supersede_results
from amdockvs.models.docking import DockingResult, InteractionsResult
from amdockvs.project.deletion import delete_docking_results
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
            result(again, "old", engine="qvina", pose_path="qvina.dock.sdf"),  # another engine
            result(again, "old", protocol="other", pose_path="protocol.pdbqt"),  # another protocol
            result(again, "new"),
        ])
        session.commit()
        # every result has a contact computed from its pose
        results = {row.id: row.pose_path for row in session.exec(select(DockingResult)).all()}
        session.add_all([InteractionsResult(docking_result_id=result_id, interaction_type="hydrophobic",
                                            residue="ALA1:A") for result_id in results])
        session.commit()
    # ...and files: its pose, the diagram drawn from it, and for Vina's SDF the PDBQT it came from
    files = [name for path in set(results.values()) for name in (path, f"{path}.rank1.diagram.json")]
    for name in [*files, "qvina.dock.pdbqt", "rec.pdb"]:
        (tmp_path / name).write_text("")
    on_disk = lambda: sorted(path.name for path in tmp_path.iterdir() if path.name in {*files, "qvina.dock.pdbqt"})

    assert supersede_results(store, run_id="") == 0
    assert supersede_results(store, run_id="new") == 2
    with store.get_session() as session:
        left = sorted((row.ligand_molecule_id, row.pose_path) for row in session.exec(select(DockingResult)).all())
    assert left == sorted([(again, "new.pdbqt"), (untouched, "old.pdbqt"), (again, "redocking.pdbqt"),
                           (again, "qvina.dock.sdf"), (again, "protocol.pdbqt")])
    assert supersede_results(store, run_id="new") == 0  # and once is enough

    def contacts():
        with store.get_session() as session:
            return sorted(results[row.docking_result_id] for row in session.exec(select(InteractionsResult)).all())

    # the contacts of a dropped result went with it, and the same goes for one deleted by hand
    assert contacts() == sorted(path for _ligand, path in left)
    by_hand = next(result_id for result_id, path in results.items() if path == "qvina.dock.sdf")
    # so far no pose went: "old.pdbqt" is still the pose of the pair not docked again. Its diagram did
    assert on_disk() == sorted(set(files) - {"old.pdbqt.rank1.diagram.json"} | {"qvina.dock.pdbqt"})
    assert delete_docking_results(store, [by_hand]) == 1
    assert contacts() == sorted(path for _ligand, path in left if path != "qvina.dock.sdf")
    assert not [name for name in on_disk() if name.startswith("qvina")]  # pose, diagram and PDBQT

    # a pose that is also a molecule's structure (a promoted hit) outlives its result; a file outside
    # the project is never touched
    outside = tmp_path.parent / "outside.sdf"
    outside.write_text("")
    with store.get_session() as session:
        session.add(MoleculeRecord(name="hit", is_ligand=True, stored_path="protocol.pdbqt"))
        elsewhere = DockingResult(receptor_molecule_id=1, ligand_molecule_id=again, engine="vina",
                                  pose_path=str(outside), created_at=datetime.now())
        session.add(elsewhere)
        session.commit()
        doomed = [elsewhere.id, *(result_id for result_id, path in results.items() if path == "protocol.pdbqt")]
    assert delete_docking_results(store, doomed) == 2
    assert outside.exists() and "protocol.pdbqt" in on_disk() and "protocol.pdbqt.rank1.diagram.json" not in on_disk()
