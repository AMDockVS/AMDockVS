"""The "already docked" guard lives in the query, not in an in-memory set.

What deserves a test: the count and the generator still agree when the filter
`id NOT IN (docked against this receptor)` is resolved by the database, and the generator is
receptor-major (a fresh ligand stream per receptor).
"""
from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.docking.jobs import count_pending_docking_pairs, iter_docking_batches
from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.pairs import iter_docking_batches_from_rows
from amdockvs.docking.results.repository import docking_run_id
from amdockvs.models.docking import DockingResult, LigandPrep, ReceptorPrep
from amdockvs.models.molecules import ImportBatch, MoleculeRecord

from payload_seed import seed_payload

ENGINE = "vina"
N_LIGANDS = 50
N_RECEPTORS = 2
N_DOCKED = 30


def _protocol_hash() -> str:
    return str(DockingProtocolMetadata.from_mapping(None).as_metrics_payload().get("hash") or "")


def _project(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    proto = _protocol_hash()
    receptor_pid = seed_payload(store, "REMARK receptor\n", "pdbqt")
    ligand_pid = seed_payload(store, "REMARK ligand\n", "pdbqt")
    with store.get_session() as session:
        receptors = []
        for i in range(N_RECEPTORS):
            rec = MoleculeRecord(name=f"rec_{i}", is_receptor=True, has_3d=True, current_payload_id=receptor_pid)
            session.add(rec)
            receptors.append(rec)
        ligands = []
        for i in range(N_LIGANDS):
            lig = MoleculeRecord(name=f"lig_{i}", is_ligand=True, current_payload_id=ligand_pid)
            session.add(lig)
            ligands.append(lig)
        session.commit()

        session.add_all(ReceptorPrep(receptor_molecule_id=row.id, rigid_payload_id=receptor_pid) for row in receptors)
        session.add_all(LigandPrep(molecule_id=row.id, payload_id=ligand_pid) for row in ligands)
        # Already docked: the first N_DOCKED ligands against the first receptor.
        run = docking_run_id(store, run_id="", engine=ENGINE, run_kind="screening", protocol={"hash": proto})
        for lig in ligands[:N_DOCKED]:
            session.add(DockingResult(
                receptor_molecule_id=receptors[0].id,
                ligand_molecule_id=lig.id,
                docking_run_id=run,
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
        preparation_engine="ad4",
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


def test_the_feed_plans_every_receptor_before_its_first_chunk(tmp_path):
    from amdockvs.screening.campaign import list_targets

    store = _project(tmp_path)
    feed = iter_docking_batches(project_db=store, output_dir=tmp_path / "out", batch_size=8,
                                engine=ENGINE, box_center=(0.0, 0.0, 0.0), box_size=(12.0, 12.0, 12.0),
                                run_id="r1")
    next(feed)  # only the first receptor's pairs exist yet
    targets = list_targets(store, run_id="r1")
    assert [(t["receptor_molecule_id"], t["ligands_total"]) for t in targets] == [(1, N_LIGANDS), (2, N_LIGANDS)]
    store.dispose()


def test_a_stopped_run_leaves_its_state_in_the_plan(tmp_path):
    """What paints a receptor's Docked bar red in Results."""
    from amdockvs.docking.jobs import _supersede
    from amdockvs.screening.campaign import list_targets

    store = _project(tmp_path)
    for run_id in ("r1", "r2"):
        next(iter_docking_batches(project_db=store, output_dir=tmp_path / run_id, batch_size=8,
                                  engine=ENGINE, box_center=(0.0, 0.0, 0.0), box_size=(12.0, 12.0, 12.0),
                                  run_id=run_id))
    _supersede({"run_id": "r1"}, {"project_db": store, "terminal_status": "canceled"})
    _supersede({"run_id": "r2"}, {"project_db": store, "terminal_status": "completed"})
    assert {t["state"] for t in list_targets(store, run_id="r1")} == {"canceled"}
    assert {t["state"] for t in list_targets(store, run_id="r2")} == {"done"}
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


def test_receptors_dock_in_id_order_like_every_table_lists_them(tmp_path):
    store = _project(tmp_path)
    with store.get_session() as session:  # the import order says the other way round
        batches = [ImportBatch(source=f"/home/u/{name}.pdb", quick_hash=name) for name in ("b", "a")]
        session.add_all(batches)
        session.flush()
        for receptor_id, batch in ((1, batches[1]), (2, batches[0])):
            row = session.get(MoleculeRecord, receptor_id)
            row.import_batch_id = batch.id
            session.add(row)
        session.commit()
    feed = iter_docking_batches(project_db=store, output_dir=tmp_path / "out", batch_size=8,
                                engine=ENGINE, box_center=(0.0, 0.0, 0.0), box_size=(12.0, 12.0, 12.0),
                                skip_existing=False)
    assert {int(pair["receptor_id"]) for pair in next(feed)["pairs"]} == {1}
    store.dispose()


def test_the_off_target_stage_docks_only_the_reference_top_n(tmp_path):
    """Rows funnel: the other receptors get the reference's best N of the scope, pose 1 only."""
    store = _project(tmp_path)
    with store.get_session() as session:
        for row in session.exec(select(DockingResult)).all():  # ligands 3..32 docked on receptor 1
            row.pose_rank, row.score = 1, -float(row.ligand_molecule_id)  # 32 is the best
        session.add(DockingResult(receptor_molecule_id=1, ligand_molecule_id=3, docking_run_id=1,
                                  pose_rank=2, score=-99.0))  # a later pose never ranks
        redock = docking_run_id(store, run_id="r", engine=ENGINE, run_kind="redocking",
                                protocol={"hash": _protocol_hash()})
        session.add(DockingResult(receptor_molecule_id=1, ligand_molecule_id=40, docking_run_id=redock,
                                  pose_rank=1, score=-99.0))  # nor does a redocking
        session.commit()

    def funnel(**scope):
        return {
            (int(pair["receptor_id"]), int(pair["ligand_id"]))
            for chunk in iter_docking_batches(
                project_db=store, output_dir=tmp_path / "out", batch_size=8, engine=ENGINE,
                box_center=(0.0, 0.0, 0.0), box_size=(12.0, 12.0, 12.0),
                selected_from_receptor_id=1, selected_top_n=5, **scope,
            )
            for pair in chunk["pairs"]
        }

    assert funnel() == {(2, ligand) for ligand in range(28, 33)}
    # The ranking happens inside the scope: dropping the best ligand brings in the 6th.
    scoped = funnel(ligand_filters={"id__not_in": [32]})
    assert scoped == {(2, ligand) for ligand in range(27, 32)}
    # By threshold: every ligand at or below the cutoff, the N is only the ceiling.
    assert funnel(selected_max_score=-30.0) == {(2, 30), (2, 31), (2, 32)}
    store.dispose()
