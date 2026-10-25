"""Screening replaces its configuration; redocking retains its protocol comparisons."""
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlmodel import select

from amdockvs.docking.api import DockingAPI, _validated_engine_config
from amdockvs.docking.engines.programs import get_docking_program
from amdockvs.docking.planning import DockingProtocol, DockingRunRequest
from amdockvs.docking.protocols import protocol_hash
from amdockvs.docking.rerun import DockingProtocolConflictError, resolved_protocol
from amdockvs.docking.results.repository import docking_run_id
from amdockvs.docking.submission import DockingSubmissionService
from amdockvs.models import DockingRun, DockingResultRecord, MoleculeRecord, ScreeningShard, ScreeningShardRun, ScreeningTarget
from amdockvs.molecules.scopes import MoleculeScope
from test_docking_skip_existing import _project


def _fixture(tmp_path, monkeypatch):
    store = _project(tmp_path)
    spec = get_docking_program("vina")
    old = resolved_protocol(spec, spec.validate_config({"exhaustiveness": 8}), None)
    with store.get_session() as session:
        run = session.get(DockingRun, 1)
        run.run_id, run.protocol, run.protocol_hash, run.protocol_label = "old", old, old["hash"], "Vina exh=8"
        session.add(run)
        for molecule in session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand == True)).all():
            molecule.has_3d, molecule.molecule_type = True, "small_molecule"
            session.add(molecule)
        session.commit()
    api = DockingAPI(SimpleNamespace(
        workspace=SimpleNamespace(project_db=store), _require_active_project=lambda: None,
    ))
    monkeypatch.setattr(api, "check_required", lambda **_kwargs: {
        "ready": True, "counts": {"ligands_ready": 50, "receptors_ready": 2},
    })
    monkeypatch.setattr("amdockvs.docking.api.batch_size_for", lambda *_args: 100)
    calls = []
    api.runtime.submit_job = lambda *args, **kwargs: calls.append(kwargs) or "job"
    return api, store, old, calls


def _results(store):
    with store.get_session() as session:
        return list(session.exec(select(DockingResultRecord)).all())


def test_changed_configuration_blocks_before_deleting_or_submitting(tmp_path, monkeypatch):
    api, store, old, calls = _fixture(tmp_path, monkeypatch)
    for change in ({"exhaustiveness": 1}, {"num_modes": 3}):
        with pytest.raises(DockingProtocolConflictError) as caught:
            api.check_protocol(program="vina", engine_config=change)
        assert caught.value.code == "docking_protocol_conflict"
        assert caught.value.previous_protocols[0]["protocol_hash"] == old["hash"]
    with pytest.raises(DockingProtocolConflictError):
        api.run(program="vina", exhaustiveness=1, receptors=MoleculeScope(filters={"id": 2}))
    assert calls == [] and len(_results(store)) == 30
    # A confirmation preflight still does not delete anything.
    api.check_protocol(program="vina", engine_config={"exhaustiveness": 1}, replace_existing=True)
    assert len(_results(store)) == 30
    store.dispose()


def test_unchanged_configuration_resumes_even_with_an_old_hash(tmp_path, monkeypatch):
    api, store, old, calls = _fixture(tmp_path, monkeypatch)
    with store.get_session() as session:
        run = session.get(DockingRun, 1)
        run.protocol_hash = "legacy-hash"
        session.add(run)
        session.commit()
    assert api.check_protocol(program="vina", engine_config=old["config"])["hash"] == "legacy-hash"
    api.run(program="vina", output_dir=tmp_path / "out", batch_size=8,
            box_center=(0, 0, 0), box_size=(12, 12, 12), replace_existing=True)
    assert len(_results(store)) == 30
    assert calls[0]["total_chunks"] == 9  # 70 pending pairs; no reset for an unchanged configuration.
    store.dispose()


def test_confirmed_change_deletes_old_docking_and_restarts_all_pairs(tmp_path, monkeypatch):
    api, store, _old, calls = _fixture(tmp_path, monkeypatch)
    other_runs = [
        docking_run_id(store, run_id="redocking", engine="vina", run_kind="redocking", protocol={"hash": "test"}),
        docking_run_id(store, run_id="other-program", engine="qvina", run_kind="screening", protocol={"hash": "qvina"}),
    ]
    with store.get_session() as session:
        session.add_all(DockingResultRecord(docking_run_id=run, receptor_molecule_id=1, ligand_molecule_id=3)
                        for run in other_runs)
        session.add(ScreeningTarget(docking_run_id=1, receptor_molecule_id=1, ligands_total=50))
        session.commit()
    api.run(program="vina", exhaustiveness=1, num_modes=3, replace_existing=True,
            output_dir=tmp_path / "out", batch_size=8, box_center=(0, 0, 0), box_size=(12, 12, 12))
    assert {row.docking_run_id for row in _results(store)} == set(other_runs)
    with store.get_session() as session:
        assert session.exec(select(ScreeningTarget)).all() == []
    assert calls[0]["total_chunks"] == 13  # all 100 pairs, rather than the previous 70 pending.
    assert calls[0]["params"]["engine_config"]["exhaustiveness"] == 1
    assert calls[0]["params"]["engine_config"]["num_modes"] == 3
    store.dispose()


def test_invalid_replacement_keeps_previous_results(tmp_path, monkeypatch):
    api, store, _old, calls = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="boolean"):
        api.run(program="vina", exhaustiveness=1, replace_existing="false")
    with pytest.raises(ValueError):
        api.run(program="vina", exhaustiveness=0, replace_existing=True)
    with pytest.raises(ValueError):
        api.run(program="vina", exhaustiveness=1, replace_existing=True,
                output_dir=tmp_path / "out", batch_size=8, box_center=(0, 0), box_size=(12, 12, 12))
    with pytest.raises(RuntimeError, match="prepared shard"):
        api.run_shards(program="vina", exhaustiveness=1, replace_existing=True,
                       output_dir=tmp_path / "out", hit_threshold=0, hit_cap=5)
    assert len(_results(store)) == 30 and calls == []
    store.dispose()


@pytest.mark.parametrize("sharded", [False, True])
def test_changing_offtarget_configuration_requires_the_reference_to_be_rerun(tmp_path, monkeypatch, sharded):
    api, store, _old, calls = _fixture(tmp_path, monkeypatch)
    kwargs = dict(program="vina", exhaustiveness=1, replace_existing=True, run_id="experiment",
                  selected_from_receptor_id=1, selected_top_n=5)
    with pytest.raises(ValueError, match="reference stage"):
        if sharded:
            api.run_shards(**kwargs, hit_threshold=0, hit_cap=5)
        else:
            api.run(**kwargs)
    assert len(_results(store)) == 30 and calls == []
    store.dispose()


def test_shard_replacement_clears_receipts_and_result_files(tmp_path, monkeypatch):
    api, store, _old, calls = _fixture(tmp_path, monkeypatch)
    fragment = tmp_path / "old.results.parquet"
    fragment.write_text("previous results")
    with store.get_session() as session:
        session.add(ScreeningShard(id=1, path="source.mshard", n_records=5))
        session.add(ScreeningShardRun(docking_run_id=1, shard_id=1, receptor_molecule_id=1,
                                     engine="vina", ligands_scored=5, result_path=fragment.name, state="done"))
        session.commit()
    monkeypatch.setattr("amdockvs.docking.api.dock_shards_job", SimpleNamespace(count_chunks=lambda *_a, **_k: 1))
    api.run_shards(program="vina", exhaustiveness=1, replace_existing=True,
                   output_dir=tmp_path / "out", hit_threshold=0, hit_cap=5)
    assert _results(store) == [] and not fragment.exists()
    with store.get_session() as session:
        assert session.exec(select(ScreeningShardRun)).all() == []
        assert len(session.exec(select(ScreeningShard)).all()) == 1
    assert len(calls) == 1
    store.dispose()


def test_shard_receipts_alone_are_enough_to_detect_a_configuration_change(tmp_path, monkeypatch):
    api, store, _old, _calls = _fixture(tmp_path, monkeypatch)
    with store.get_session() as session:
        session.exec(delete(DockingResultRecord))
        session.add(ScreeningShard(id=1, path="source.mshard", n_records=5))
        session.add(ScreeningShardRun(docking_run_id=1, shard_id=1, receptor_molecule_id=1,
                                     engine="vina", ligands_scored=5, state="done"))
        session.commit()
    with pytest.raises(DockingProtocolConflictError):
        api.check_protocol(program="vina", engine_config={"exhaustiveness": 1})
    store.dispose()


def test_api_knobs_are_resolved_and_cpu_does_not_change_configuration_identity():
    program = get_docking_program("qvina")
    config = _validated_engine_config(program, None, exhaustiveness=1, num_modes=3)
    assert config["exhaustiveness"] == 1 and config["num_modes"] == 3
    assert _validated_engine_config(get_docking_program("gnina"), None, scoring_function="vina")["scoring_function"] == "rescore"
    assert protocol_hash(program="qvina", config=config) == protocol_hash(
        program="qvina", config={**config, "vina_cpu": 16},
    )


def test_offtarget_replacement_happens_only_in_the_reference_stage():
    calls = []
    runtime = SimpleNamespace(
        docking=SimpleNamespace(check_protocol=lambda **_kwargs: None,
                                run=lambda **kwargs: calls.append(kwargs) or f"job-{len(calls)}"),
        molecules=SimpleNamespace(filter=lambda scope, **_kwargs: scope),
    )
    request = DockingRunRequest(run_kind="docking", ligand_scope=object(), receptor_scope=object(),
                               protocols=(DockingProtocol("vina", "Vina"),), replace_existing=True,
                               offtarget_reference_id=1, offtarget_top_n=5)
    DockingSubmissionService(runtime).submit(request, ready_receptor_ids=(1, 2))
    assert [call["replace_existing"] for call in calls] == [True, False]
    assert calls[1]["depends_on"] == ["job-1"]


def test_multiple_configurations_per_program_are_only_allowed_for_redocking():
    calls = []
    runtime = SimpleNamespace(docking=SimpleNamespace(redock=lambda **kwargs: calls.append(kwargs) or "job"))
    protocols = (DockingProtocol("vina", "A", config={"exhaustiveness": 1}),
                 DockingProtocol("vina", "B", config={"exhaustiveness": 8}))
    with pytest.raises(ValueError, match="one configuration"):
        DockingSubmissionService(runtime).submit(DockingRunRequest(
            run_kind="docking", ligand_scope=object(), receptor_scope=object(), protocols=protocols,
        ))
    DockingSubmissionService(runtime).submit(DockingRunRequest(
        run_kind="redocking", ligand_scope=None, receptor_scope=None, protocols=protocols, complex_ids=(1,),
    ))
    assert len(calls) == 2


@pytest.mark.parametrize("confirmed", [False, True])
def test_ui_requires_confirmation_of_deletion(monkeypatch, confirmed):
    from amdockvs.ui.tools.docking import run_panel

    class Dialog:
        Icon = SimpleNamespace(Warning=1)
        ButtonRole = SimpleNamespace(DestructiveRole=1)
        StandardButton = SimpleNamespace(Cancel="Cancel")
        def __init__(self, _parent):
            self.buttons = []
        def __getattr__(self, _name):
            return lambda *_args: None
        def addButton(self, *_args):
            button = object()
            self.buttons.append(button)
            return button
        def clickedButton(self):
            assert len(self.buttons) == 2
            return self.buttons[0 if confirmed else 1]

    monkeypatch.setattr(run_panel, "QMessageBox", Dialog)
    calls = []
    panel = SimpleNamespace(run_button=SimpleNamespace(setEnabled=lambda _value: None),
                            _after_conflict_check=lambda *args: calls.append(args))
    exc = DockingProtocolConflictError(requested={"program": "vina"}, previous=[{"protocol_label": "old"}])
    params = {"run_id": "new"}
    run_panel.RunPanel._on_docking_error(panel, exc, inp={}, params=params, signature="signature")
    assert len(calls) == int(confirmed)
    if confirmed:
        assert calls[0][2] == {"run_id": "new", "replace_existing": True}
    assert params == {"run_id": "new"}
