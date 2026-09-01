"""The "already docked" guard lives in the query, not in an in-memory set.

What deserves a test: the count and the generator still agree when the filter
`id NOT IN (docked against this receptor)` is resolved by the database, and the generator is
receptor-major (a fresh ligand stream per receptor).
"""
from datetime import datetime

from ms_flow.core.database.project import ProjectStore

from amdockvs.docking.jobs import count_pending_docking_pairs, iter_docking_batches
from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.service import iter_docking_batches_from_rows
from amdockvs.models.docking import DockingResult, EngineState
from amdockvs.models.molecules import MoleculeRecord

ENGINE = "vina"
N_LIGANDS = 50
N_RECEPTORS = 2
N_DOCKED = 30


def _protocol_hash() -> str:
    return str(DockingProtocolMetadata.from_mapping(None).as_metrics_payload().get("hash") or "")


def _project(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    proto = _protocol_hash()
    with store.get_session() as session:
        receptors = []
        for i in range(N_RECEPTORS):
            rec = MoleculeRecord(name=f"rec_{i}", is_receptor=True, stored_path=f"rec_{i}.pdb")
            session.add(rec)
            receptors.append(rec)
        ligands = []
        for i in range(N_LIGANDS):
            lig = MoleculeRecord(name=f"lig_{i}", is_ligand=True, stored_path=f"lig_{i}.sdf")
            session.add(lig)
            ligands.append(lig)
        session.commit()

        for role, rows in (("receptor", receptors), ("ligand", ligands)):
            for row in rows:
                session.add(EngineState(molecule_id=row.id, role_type=role, engine=ENGINE, is_ready=True))
        # Already docked: the first N_DOCKED ligands against the first receptor.
        for lig in ligands[:N_DOCKED]:
            session.add(DockingResult(
                receptor_molecule_id=receptors[0].id,
                ligand_molecule_id=lig.id,
                engine=ENGINE,
                metrics={"run_kind": "screening", "protocol": {"hash": proto}},
                created_at=datetime.now(),
            ))
        session.commit()
    return store


def _emitted(store, tmp_path, *, skip_existing):
    pairs = set()
    for chunk in iter_docking_batches(
        project_db=store,
        output_dir=tmp_path / "out",
        batch_size=8,
        engine=ENGINE,
        box_center=(0.0, 0.0, 0.0),
        box_size=(12.0, 12.0, 12.0),
        skip_existing=skip_existing,
    ):
        for pair in chunk["pairs"]:
            pairs.add((int(pair["receptor_id"]), int(pair["ligand_id"])))
    return pairs


def _counted(store, *, skip_existing):
    return count_pending_docking_pairs(
        project_db=store,
        engine=ENGINE,
        preparation_engine=ENGINE,
        ligand_set_id=None,
        receptor_set_id=None,
        ligand_filters=None,
        receptor_filters=None,
        protocol_metadata=None,
        skip_existing=skip_existing,
    )


def test_count_and_generator_agree_on_the_pending_pairs(tmp_path):
    store = _project(tmp_path)
    total = N_LIGANDS * N_RECEPTORS
    assert _counted(store, skip_existing=True) == total - N_DOCKED == 70
    emitted = _emitted(store, tmp_path, skip_existing=True)
    assert len(emitted) == 70
    # ids: receptors 1..2, ligands 3..52; the docked ones are receptor 1 x ligands 3..32.
    assert emitted.isdisjoint({(1, i) for i in range(3, 3 + N_DOCKED)})
    assert sum(1 for receptor_id, _ in emitted if receptor_id == 2) == N_LIGANDS
    store.dispose()


def test_without_skip_the_whole_cross_product_is_emitted(tmp_path):
    store = _project(tmp_path)
    assert _counted(store, skip_existing=False) == N_LIGANDS * N_RECEPTORS
    assert len(_emitted(store, tmp_path, skip_existing=False)) == N_LIGANDS * N_RECEPTORS
    store.dispose()


def test_receptor_major_needs_a_re_iterable_ligand_source():
    ligands = [{"id": 1}, {"id": 2}]
    receptors = [{"id": 10}, {"id": 11}]

    def run(source):
        pairs = set()
        for chunk in iter_docking_batches_from_rows(
            ligands=source,
            receptors=receptors,
            output_dir="/tmp/amdock_skip_test",
            batch_size=100,
            box_center=(0.0, 0.0, 0.0),
            box_size=(12.0, 12.0, 12.0),
        ):
            for pair in chunk["pairs"]:
                pairs.add((int(pair["receptor_id"]), int(pair["ligand_id"])))
        return pairs

    assert run(ligands) == {(10, 1), (10, 2), (11, 1), (11, 2)}
    assert run(lambda receptor_id: ligands) == {(10, 1), (10, 2), (11, 1), (11, 2)}
    # A plain iterator would be exhausted after the first receptor: that is an error, not half a job.
    try:
        run(iter(ligands))
    except TypeError:
        pass
    else:
        raise AssertionError("a plain iterator must be rejected")
