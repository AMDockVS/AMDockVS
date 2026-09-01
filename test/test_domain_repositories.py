"""queries.py dissolved: the result queries live in docking/repository.py and the table
statistics in molecules/repository.py, aggregating in SQL instead of reading the table.
"""
from datetime import datetime

from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.docking import repository
from amdockvs.models.docking import DockingResult
from amdockvs.models.molecules import MoleculeRecord
from amdockvs.molecules.repository import ligand_table_stats, receptor_table_stats

ENGINE = "vina"


def _project(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        rec = MoleculeRecord(name="rec", is_receptor=True, source="/data/rec.pdb", n_atoms=100,
                             input_format="pdb", has_3d=True)
        ligands = [
            MoleculeRecord(name=f"lig_{i}", is_ligand=True, source="/data/libs.sdf", source_index=i,
                           n_atoms=10 + i, input_format="sdf", excluded=(i == 2))
            for i in range(3)
        ]
        session.add(rec)
        for lig in ligands:
            session.add(lig)
        session.commit()
        for rank in (1, 2):
            for lig, score in zip(ligands[:2], (-9.5, -7.0)):
                session.add(DockingResult(
                    receptor_molecule_id=rec.id, ligand_molecule_id=lig.id, engine=ENGINE,
                    pose_rank=rank, score=score - rank, pose_path=f"poses/{lig.id}_{rank}.pdbqt",
                    metrics={"run_kind": "screening", "protocol": {"hash": "h1", "label": "Vina"}},
                    created_at=datetime.now(),
                ))
        session.commit()
    return store


def test_docking_result_queries_survive_the_move(tmp_path):
    store = _project(tmp_path)
    stats = repository.get_docking_results_stats(store)
    assert stats.total_results == 2 and stats.unique_receptors == 1
    assert stats.best_score == -11.5

    hits = repository.list_results(store)
    assert len(hits) == 2  # one row per pair: only pose_rank = 1
    assert hits[0].protocol_label == "Vina" and hits[0].receptor_name == "rec"
    assert repository.get_hit(store, result_id=hits[0].result_id) == hits[0]
    assert repository.get_hit(store, result_id=999_999) is None

    assert repository.count_docked_pairs(store, engine=ENGINE, protocol_hash="h1") == 2
    assert repository.count_docked_pairs(store, engine=ENGINE, receptor_ids=[]) == 0
    assert repository.pivot_availability(store) == {"hits": True, "redocking": False, "offtarget": False}
    assert [label for _, label in repository.list_result_protocols(store)] == ["Vina"]
    assert len(repository.list_offtarget_rows(store)) == 4  # every pose
    assert repository.list_receptor_result_summaries(store)[0].total_results == 2
    assert len(repository.list_docking_result_rows(store, pose_rank=1)) == 2

    with store.get_session() as session:
        ligand = session.exec(select(MoleculeRecord).where(MoleculeRecord.name == "lig_0")).one()
        other = MoleculeRecord(name="rec_2", is_receptor=True)
        session.add(other)
        session.commit()
        session.add_all([
            DockingResult(
                receptor_molecule_id=other.id,
                ligand_molecule_id=ligand.id,
                engine=ENGINE,
                pose_rank=1,
                score=-8.0,
                metrics={"run_kind": "screening"},
            ),
            DockingResult(
                receptor_molecule_id=other.id,
                ligand_molecule_id=ligand.id,
                engine=ENGINE,
                pose_rank=2,
                score=-7.0,
                metrics={"run_kind": "redocking"},
            ),
        ])
        session.commit()
    assert repository.pivot_availability(store) == {
        "hits": True,
        "redocking": True,
        "offtarget": True,
    }
    store.dispose()


def test_table_stats_aggregate_in_sql(tmp_path):
    store = _project(tmp_path)
    ligands = ligand_table_stats(store)
    assert ligands.total_ligands == 3
    assert dict((item.value, item.count) for item in ligands.by_status) == {"active": 2, "excluded": 1}
    assert ligands.by_input_format[0].value == "sdf"
    assert ligands.by_source_file[0].count == 3 and ligands.by_source_file[0].max_source_index == 2
    assert (ligands.atoms.min, ligands.atoms.max) == (10, 12) and ligands.atoms.avg == 11.0

    receptors = receptor_table_stats(store)
    assert receptors.total_receptors == 1
    assert [item.value for item in receptors.by_status] == ["imported"]
    store.dispose()
