"""A forced re-dock replaces a result only once it has another one for the same docking, and a result
that goes takes what was computed from it."""
from datetime import datetime

from ms_flow.core.database.project import ProjectStore
from payload_seed import seed_payload
from sqlmodel import select

from amdockvs.core.constants import SERIES_POSES
from amdockvs.core.payloads import PAYLOAD_CACHE
from amdockvs.docking.results.repository import supersede_results
from amdockvs.models.docking import DockingResult, InteractionsResult
from amdockvs.project.deletion import delete_docking_results
from amdockvs.models.molecules import MoleculeRecord


def test_only_the_pairs_docked_again_lose_their_older_rows(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    names = ["old_a", "old_b", "untouched", "redocking", "qvina", "protocol", "new"]
    pose = {name: seed_payload(store, f"{name}\n", "sdf", SERIES_POSES) for name in names}
    label = {pid: name for name, pid in pose.items()}
    with store.get_session() as session:
        receptor = MoleculeRecord(name="rec", is_receptor=True)
        ligands = [MoleculeRecord(name=f"lig_{i}", is_ligand=True) for i in range(2)]
        session.add_all([receptor, *ligands])
        session.commit()
        again, untouched = ligands[0].id, ligands[1].id

        def result(ligand, run, name, *, engine="vina", kind="screening", protocol="h"):
            return DockingResult(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand, engine=engine,
                                 pose_payload_id=pose[name], created_at=datetime.now(),
                                 metrics={"run_id": run, "run_kind": kind, "protocol": {"hash": protocol}})

        session.add_all([
            result(again, "old", "old_a"), result(again, "old", "old_b"),  # the two poses the new run replaces
            result(untouched, "old", "untouched"),  # a pair the new run did not get to
            result(again, "old", "redocking", kind="redocking"),  # another kind of run
            result(again, "old", "qvina", engine="qvina"),  # another engine
            result(again, "old", "protocol", protocol="other"),  # another protocol
            result(again, "new", "new"),
        ])
        session.commit()
        # every result has a contact computed from its pose
        results = {row.id: label[row.pose_payload_id] for row in session.exec(select(DockingResult)).all()}
        session.add_all([InteractionsResult(docking_result_id=result_id, interaction_type="hydrophobic",
                                            residue="ALA1:A") for result_id in results])
        session.commit()
    # ...and files someone opened: the cached pose and the diagram drawn from it
    cache = tmp_path / PAYLOAD_CACHE
    cache.mkdir(parents=True)
    for pid in pose.values():
        for name in (f"{pid}.sdf", f"{pid}.sdf.rank1.diagram.json"):
            (cache / name).write_text("")
    cached = lambda: sorted({label[int(path.name.split(".")[0])] for path in cache.iterdir()})
    stored = lambda: sorted(label[pid] for pid in store.payloads.kinds(pose.values()))

    assert supersede_results(store, run_id="") == 0
    assert supersede_results(store, run_id="new") == 2
    with store.get_session() as session:
        left = sorted(label[row.pose_payload_id] for row in session.exec(select(DockingResult)).all())
    assert left == ["new", "protocol", "qvina", "redocking", "untouched"]
    assert supersede_results(store, run_id="new") == 0  # and once is enough

    def contacts():
        with store.get_session() as session:
            return sorted(results[row.docking_result_id] for row in session.exec(select(InteractionsResult)).all())

    # the contacts, the pose payload and the cached files of a dropped result went with it
    assert contacts() == left
    assert stored() == left and cached() == left
    by_hand = next(result_id for result_id, name in results.items() if name == "qvina")
    assert delete_docking_results(store, [by_hand]) == 1
    assert contacts() == stored() == cached() == [name for name in left if name != "qvina"]

    # a pose that is also a molecule's structure (a promoted hit) outlives its result
    with store.get_session() as session:
        session.add(MoleculeRecord(name="hit", is_ligand=True, stored_payload_id=pose["protocol"]))
        session.commit()
    doomed = [result_id for result_id, name in results.items() if name == "protocol"]
    assert delete_docking_results(store, doomed) == 1
    assert "protocol" in stored() and "protocol" in cached()
    store.dispose()
