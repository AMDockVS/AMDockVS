"""skip_existing reuses a result only for the inputs it was docked against (report Fig2 F2-02).

A result keeps what its pair was docked against as `input_key`: the receptor's docked input, its
flexible side chains and the box. Another box, or the receptor prepared again, docks the pair again
and the stale result goes when the run ends; the same inputs skip it as before.
"""
from types import SimpleNamespace

from ms_flow.core.database.project import ProjectStore
from sqlmodel import select

from amdockvs.docking import tasks
from amdockvs.docking.api import DockingAPI
from amdockvs.docking.jobs import (
    _supersede,
    _with_docking_run,
    count_pending_docking_pairs,
    count_pending_redocking_pairs,
    iter_docking_batches,
    iter_redocking_batches,
)
from amdockvs.docking.pairs import legacy_input_key
from amdockvs.docking.results.repository import docking_run_id
from amdockvs.models.complexes import ComplexRecord
from amdockvs.models.docking import BindingSite, DockingResult, LigandPrep, ReceptorPrep
from amdockvs.models.molecules import MoleculeRecord

from payload_seed import seed_payload

N_LIGANDS = 6
CENTER = (1.0, 2.0, 3.0)
SIZE = (20.0, 20.0, 20.0)


def _project(tmp_path):
    store = ProjectStore.open_at(tmp_path / "project.db")
    receptor_pid = seed_payload(store, "REMARK receptor\n", "pdbqt")
    ligand_pid = seed_payload(store, "REMARK ligand\n", "pdbqt")
    with store.get_session() as session:
        receptor = MoleculeRecord(name="rec", is_receptor=True, has_3d=True, current_payload_id=receptor_pid)
        ligands = [MoleculeRecord(name=f"lig_{i}", is_ligand=True, current_payload_id=ligand_pid)
                   for i in range(N_LIGANDS)]
        session.add_all([receptor, *ligands])
        session.commit()
        session.add(ReceptorPrep(receptor_molecule_id=receptor.id, rigid_payload_id=receptor_pid))
        session.add_all(LigandPrep(molecule_id=row.id, payload_id=ligand_pid) for row in ligands)
        session.add(BindingSite(molecule_id=receptor.id, is_active=True, center_x=CENTER[0], center_y=CENTER[1],
                                center_z=CENTER[2], size_x=SIZE[0], size_y=SIZE[1], size_z=SIZE[2]))
        session.commit()
    return store


def _fake_engine(monkeypatch):
    """One pose per pair, as an engine's rows come back: no program runs."""
    monkeypatch.setattr(tasks, "run_docking_chunk", lambda payload, _progress=None: [
        {"receptor_molecule_id": pair["receptor_id"], "ligand_molecule_id": pair["ligand_id"],
         "pose_rank": 1, "score": -7.0} for pair in payload["pairs"]])


def _counted(store, **box):
    return count_pending_docking_pairs(
        project_db=store, engine="vina", preparation_engine="ad4", ligand_set_id=None, receptor_set_id=None,
        ligand_filters=None, receptor_filters=None, protocol_metadata=None, skip_existing=True, **box)


def _dock(store, tmp_path, run_id, **box) -> int:
    """A screening run with skip_existing, end to end but for the engine: the pairs its feed emits
    become rows as the worker writes them, and its end supersedes. Returns how many pairs it docked,
    after checking the declared count agreed."""
    expected = _counted(store, **box)
    docked = 0
    feed = iter_docking_batches(project_db=store, output_dir=tmp_path / run_id, batch_size=4, engine="vina",
                                run_id=run_id, skip_existing=True, **box)
    with store.get_session() as session:
        for chunk in _with_docking_run(feed, store, "screening"):
            rows = tasks.dock_pairs(chunk)
            docked += len(rows)
            session.add_all(DockingResult(**DockingResult.build_row(**row)) for row in rows)
        session.commit()
    _supersede({"run_id": run_id, "skip_existing": True}, {"project_db": store})
    assert docked == expected
    return docked


def _results(store):
    with store.get_session() as session:
        return list(session.exec(select(DockingResult)).all())


def _move_site(store, dx):
    with store.get_session() as session:
        site = session.exec(select(BindingSite)).one()
        site.center_x = CENTER[0] + dx
        session.add(site)
        session.commit()


def _prepare_again(store, text="REMARK receptor, dry\n"):
    new_pid = seed_payload(store, text, "pdbqt")
    with store.get_session() as session:
        prep = session.exec(select(ReceptorPrep)).one()
        prep.rigid_payload_id = new_pid
        session.add(prep)
        session.commit()
    return new_pid


def test_same_inputs_are_skipped(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    assert _dock(store, tmp_path, "r1") == N_LIGANDS
    assert len({row.input_key for row in _results(store)}) == 1 and _results(store)[0].input_key
    assert _dock(store, tmp_path, "r2") == 0
    store.dispose()


def test_a_moved_stored_box_docks_again_and_replaces_the_stale_results(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    _dock(store, tmp_path, "r1")
    old_key = _results(store)[0].input_key
    _move_site(store, 100.0)
    assert _dock(store, tmp_path, "r2") == N_LIGANDS
    results = _results(store)
    assert len(results) == N_LIGANDS  # the old box's poses are gone, not mixed with the new ones
    assert {row.input_key for row in results} != {old_key}
    _move_site(store, 0.0)  # and back: the results now are for the moved box
    assert _dock(store, tmp_path, "r3") == N_LIGANDS
    store.dispose()


def test_an_explicit_box_is_part_of_the_inputs(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    box = {"box_center": CENTER, "box_size": SIZE}
    assert _dock(store, tmp_path, "r1", **box) == N_LIGANDS
    assert _dock(store, tmp_path, "r2", **box) == 0
    moved = {"box_center": (CENTER[0] + 100.0, CENTER[1], CENTER[2]), "box_size": SIZE}
    assert _dock(store, tmp_path, "r3", **moved) == N_LIGANDS
    store.dispose()


def test_a_receptor_prepared_again_docks_again(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    _dock(store, tmp_path, "r1")
    old_pid = _results(store)[0].receptor_payload_id
    new_pid = _prepare_again(store)
    assert _dock(store, tmp_path, "r2") == N_LIGANDS
    assert {row.receptor_payload_id for row in _results(store)} == {new_pid} != {old_pid}
    store.dispose()


def test_an_older_projects_results_are_kept_until_the_receptor_changes(tmp_path, monkeypatch):
    """Results from before `input_key` know their receptor payload, not their box (`_add_columns`)."""
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    with store.get_session() as session:
        prep = session.exec(select(ReceptorPrep)).one()
        ligands = session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand == True)).all()  # noqa: E712
        run = docking_run_id(store, run_id="old", engine="vina", run_kind="screening",
                             protocol={"hash": _hash()})
        session.add_all(DockingResult(receptor_molecule_id=prep.receptor_molecule_id, ligand_molecule_id=row.id,
                                      docking_run_id=run, receptor_payload_id=prep.rigid_payload_id,
                                      input_key=legacy_input_key(prep.rigid_payload_id)) for row in ligands)
        session.commit()
    assert _dock(store, tmp_path, "r1") == 0
    _prepare_again(store)
    assert _dock(store, tmp_path, "r2") == N_LIGANDS
    store.dispose()


def _hash():
    from amdockvs.docking.protocols import DockingProtocolMetadata
    return str(DockingProtocolMetadata.from_mapping(None).as_metrics_payload().get("hash") or "")


def test_the_preview_counts_only_results_for_the_current_inputs(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    _dock(store, tmp_path, "r1")
    api = DockingAPI(SimpleNamespace(workspace=SimpleNamespace(project_db=store), _require_active_project=lambda: None))
    receptor = _results(store)[0].receptor_molecule_id
    count = lambda: api.count_docked_pairs(engine="vina", receptor_ids=[receptor], program="vina")  # noqa: E731
    assert count() == N_LIGANDS
    _move_site(store, 100.0)
    assert count() == 0
    assert api.count_docked_pairs(engine="vina", receptor_ids=[receptor]) == N_LIGANDS  # without a program: any
    store.dispose()


def test_redocking_docks_a_complex_again_when_its_box_moves(tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    with store.get_session() as session:
        receptor = session.exec(select(MoleculeRecord).where(MoleculeRecord.is_receptor == True)).first()  # noqa: E712
        ligand = session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand == True)).first()  # noqa: E712
        session.add(ComplexRecord(receptor_molecule_id=receptor.id, ligand_molecule_id=ligand.id, purpose="redocking"))
        session.commit()

    def redock(run_id):
        pending = count_pending_redocking_pairs(project_db=store, engine="vina", complex_set_id=None, complex_ids=None,
                                                purpose="redocking", protocol_metadata=None, skip_existing=True)
        docked = 0
        feed = iter_redocking_batches(project_db=store, output_dir=tmp_path / run_id, batch_size=4, engine="vina",
                                      run_id=run_id, skip_existing=True)
        with store.get_session() as session:
            for chunk in _with_docking_run(feed, store, "redocking"):
                rows = tasks.dock_pairs(chunk)
                docked += len(rows)
                session.add_all(DockingResult(**DockingResult.build_row(**row)) for row in rows)
            session.commit()
        assert docked == pending
        return docked

    assert redock("d1") == 1
    assert redock("d2") == 0
    _move_site(store, 100.0)
    assert redock("d3") == 1
    store.dispose()


def test_opening_an_older_project_keys_its_results_by_receptor_payload(tmp_path):
    from amdockvs.runtime import AMDockVSRuntime

    store = _project(tmp_path)
    with store.get_session() as session:
        prep = session.exec(select(ReceptorPrep)).one()
        run = docking_run_id(store, run_id="old", engine="vina", run_kind="screening", protocol={"hash": "h"})
        session.add(DockingResult(receptor_molecule_id=prep.receptor_molecule_id, ligand_molecule_id=2,
                                  docking_run_id=run, receptor_payload_id=prep.rigid_payload_id))
        session.add(DockingResult(receptor_molecule_id=prep.receptor_molecule_id, ligand_molecule_id=3,
                                  docking_run_id=run))
        session.commit()
        session.connection().exec_driver_sql("ALTER TABLE docking_results DROP COLUMN input_key")
        session.commit()
        payload = prep.rigid_payload_id
    AMDockVSRuntime._add_columns(SimpleNamespace(workspace=SimpleNamespace(project_db=store)))
    assert sorted(row.input_key for row in _results(store)) == sorted([legacy_input_key(payload), "legacy:0"])
    store.dispose()


def test_a_campaign_keys_its_receptors_as_a_row_fed_run_does(tmp_path, monkeypatch):
    """A sharded campaign's hits become results too: keyed the same, a later run over those
    ligands skips them for the same receptor and box, and docks them again for another."""
    from amdockvs.docking import shard_jobs

    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    _dock(store, tmp_path, "r1")
    key = _results(store)[0].input_key
    params = shard_jobs.DockShardsJobParams(hit_threshold=-5.0, hit_cap=10)
    targets = lambda: shard_jobs.receptor_targets(store, params, tmp_path / "campaign")  # noqa: E731
    assert [target["input_key"] for target in targets()] == [key]
    _move_site(store, 100.0)
    assert [target["input_key"] for target in targets()] != [key]
    store.dispose()


def test_a_sites_own_spacing_is_what_both_the_run_and_the_preview_key_by(tmp_path, monkeypatch):
    """The grid's spacing wins over any default, so the preview agrees with the feed: it would
    otherwise count a docked pair as pending and the job would wait on a chunk never emitted."""
    _fake_engine(monkeypatch)
    store = _project(tmp_path)
    with store.get_session() as session:
        site = session.exec(select(BindingSite)).one()
        site.extra_data = {"spacing": 0.5}
        session.add(site)
        session.commit()
    _dock(store, tmp_path, "r1")  # asserts the declared count matches what the feed emits
    api = DockingAPI(SimpleNamespace(workspace=SimpleNamespace(project_db=store), _require_active_project=lambda: None))
    receptor = _results(store)[0].receptor_molecule_id
    assert api.count_docked_pairs(engine="vina", receptor_ids=[receptor], program="vina") == N_LIGANDS
    assert _counted(store) == 0  # nothing left pending
    store.dispose()
