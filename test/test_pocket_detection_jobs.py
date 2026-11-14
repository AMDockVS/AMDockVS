"""Pocket Detection follows P2Rank work continued through the Jobs monitor."""
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6
from amdockvs.binding_sites.jobs import p2rank_prediction_job
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from ms_components.ms_table import ColumnDef, SmartTableView, TableConfig
from ms_components.ms_monitor.models import JobMonitorState, ProjectMonitorState
from ms_flow.core.database import ProjectStore
from amdockvs.binding_sites.api import BindingSiteAPI, P2RankPredictionPlan
from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.ui.tools.binding_sites.detection import BindingSitesWidget, PocketDetectionWidget


class _Bridge(QObject):
    job_upserted = Signal(str, object)
    job_finished = Signal(str, str)

    def __init__(self, jobs):
        super().__init__()
        self.current_snapshot = ProjectMonitorState(project_id="project", has_project=True, jobs=jobs)


def _job(job_id="pockets", status="running", name=None):
    return JobMonitorState(job_id=job_id, status=status, name=name or p2rank_prediction_job.name)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(PocketDetectionWidget, "refresh", lambda self: None)
    monkeypatch.setattr(PocketDetectionWidget, "_reload_sites", lambda self: None)
    host = QWidget()
    host.monitor_bridge = _Bridge([])
    active = []
    api = SimpleNamespace(
        predict=Mock(return_value="new-job"),
        plan_prediction=Mock(return_value=SimpleNamespace(
            recalculate_receptor_ids=(1,), reused_receptor_ids=(),
            missing_structure_ids=(), missing_structure_names=(),
        )),
    )
    runtime = SimpleNamespace(binding_sites=api, list_jobs=lambda **kwargs: list(active))
    widget = PocketDetectionWidget(runtime=runtime, parent=host)
    widget._receptors = {1: {"id": 1}}
    widget._tool_ready = True
    widget.run_button.setEnabled(True)
    yield widget, host.monitor_bridge, active, api
    host.close()
    host.deleteLater()
    QApplication.processEvents()


def test_resumed_job_disables_run_and_its_completion_reenables_it(panel):
    widget, bridge, active, api = panel
    widget._pending_job_ids.add("pockets")
    bridge.job_finished.emit("pockets", "canceled")
    assert widget.run_button.isEnabled()

    bridge.job_upserted.emit("pockets", _job())
    active.append(SimpleNamespace(job_id="pockets", task_type=p2rank_prediction_job.name))
    assert not widget.run_button.isEnabled()
    assert widget._pending_job_ids == {"pockets"}
    assert "in progress" in widget.status_label.text()
    assert not widget.job_details_link.isHidden()
    widget._submit_prediction()
    api.plan_prediction.assert_not_called()
    api.predict.assert_not_called()

    # An asynchronous receptor refresh must not enable Run while work is resumed.
    widget._apply_refresh((SimpleNamespace(installed=True, java_version=17, message="Ready"), [{"id": 1}]))
    assert not widget.run_button.isEnabled()
    active.clear()
    bridge.job_finished.emit("pockets", "completed")
    assert widget.run_button.isEnabled()
    assert not widget._pending_job_ids


def test_submission_checks_live_jobs_before_the_resume_update_arrives(panel):
    widget, _bridge, active, api = panel
    assert not widget._pending_job_ids
    active.append(SimpleNamespace(job_id="resumed", task_type=p2rank_prediction_job.name))
    widget._submit_prediction()
    assert not widget.run_button.isEnabled()
    assert widget._pending_job_ids == {"resumed"}
    api.plan_prediction.assert_not_called()
    api.predict.assert_not_called()


def test_new_submission_is_guarded_before_its_first_monitor_update(panel):
    widget, _bridge, _active, api = panel
    widget._submit_prediction()
    widget._submit_prediction()
    api.predict.assert_called_once()
    assert widget._pending_job_ids == {"new-job"}
    assert not widget.run_button.isEnabled()


@pytest.mark.parametrize("profile", ["auto", "alphafold", "default"])
def test_submitting_sends_one_profile_and_reads_no_structures(panel, profile):
    """Thousands of receptors must not be fetched before a job can start.

    The executor resolves each receptor's profile against the structure it already has to
    write for P2Rank, so the panel sends the choice and nothing else.
    """
    widget, _bridge, _active, api = panel
    assert widget.profile_combo.currentData() == "auto"  # decided per receptor, on the executor
    widget.profile_combo.setCurrentIndex(widget.profile_combo.findData(profile))

    widget._submit_prediction()

    for call in (api.plan_prediction.call_args, api.predict.call_args):
        assert call.kwargs["profile"] == profile
        assert "profiles" not in call.kwargs


def test_a_pocket_explains_a_profile_it_did_not_ask_for(panel):
    """The Profile column carries what ran; its tooltip carries why."""
    from amdockvs.binding_sites.p2rank import B_FACTOR_CONSTANT
    from amdockvs.ui.tools.binding_sites.detection import PROFILE_REASON_HINTS

    widget, _bridge, _active, _api = panel
    sites = BindingSitesWidget(runtime=widget.runtime, parent=None)
    sites._fill_results((
        BindingSite(id=1, molecule_id=1, name="P2Rank pocket 1", source="p2rank", source_ref="1",
                    extra_data={"rank": 1, "profile": "alphafold",
                                "profile_reason": B_FACTOR_CONSTANT}),
        BindingSite(id=2, molecule_id=1, name="P2Rank pocket 2", source="p2rank", source_ref="2",
                    extra_data={"rank": 2, "profile": "default"}),
    ))

    assert sites.table.item(0, 4).text() == "alphafold"
    assert sites.table.item(0, 4).toolTip() == PROFILE_REASON_HINTS[B_FACTOR_CONSTANT]
    assert sites.table.item(1, 4).text() == "default"
    assert "B-factors ignored" not in sites.table.item(1, 4).toolTip()
    sites.deleteLater()


def test_unrelated_jobs_do_not_block_prediction(panel):
    widget, bridge, active, api = panel
    bridge.job_upserted.emit("import", _job(job_id="import", name="amdock_load_receptors_file_job"))
    active.append(SimpleNamespace(job_id="import", task_type="amdock_load_receptors_file_job"))
    widget._submit_prediction()
    api.predict.assert_called_once()


def test_one_finished_job_does_not_enable_run_while_another_is_active(panel):
    widget, bridge, _active, _api = panel
    bridge.job_upserted.emit("first", _job(job_id="first"))
    bridge.job_upserted.emit("second", _job(job_id="second", status="waiting"))
    bridge.job_finished.emit("first", "completed")
    assert not widget.run_button.isEnabled()
    assert widget._pending_job_ids == {"second"}
    bridge.job_finished.emit("second", "canceled")
    assert widget.run_button.isEnabled()


def test_opening_panel_adopts_active_p2rank_jobs_from_snapshot(panel):
    widget, bridge, _active, _api = panel
    widget._job_signal_connected = False
    bridge.job_upserted.disconnect(widget._on_job_upserted)
    bridge.job_finished.disconnect(widget._on_job_finished)
    bridge.current_snapshot.jobs = [_job(status="waiting"), _job(job_id="done", status="completed")]
    widget._connect_job_signal()
    assert widget._pending_job_ids == {"pockets"}
    assert not widget.run_button.isEnabled()


def test_canceled_job_can_be_submitted_again_when_not_resumed(panel):
    widget, bridge, _active, api = panel
    widget._pending_job_ids.add("pockets")
    bridge.job_finished.emit("pockets", "canceled")
    widget._submit_prediction()
    api.predict.assert_called_once()


def test_tool_settings_link_opens_external_tools(panel, monkeypatch):
    widget, _bridge, _active, _api = panel
    settings = Mock()
    monkeypatch.setattr(widget.window(), "open_settings", settings, raising=False)
    widget._open_tool_settings()
    settings.assert_called_once_with(node="page:External tools")


def test_submitting_reveals_a_link_that_opens_the_jobs_monitor(panel, monkeypatch):
    widget, _bridge, _active, _api = panel
    monitor = Mock()
    monkeypatch.setattr(widget.window(), "open_jobs_monitor", monitor, raising=False)
    assert widget.job_details_link.isHidden()
    widget._submit_prediction()
    assert not widget.job_details_link.isHidden()
    widget.job_details_link.linkActivated.emit("jobs")
    monitor.assert_called_once_with()


@pytest.mark.parametrize("selected_row", [None, 1])
@pytest.mark.parametrize("reuse", [False, True])
def test_results_appear_without_switching_tabs(tmp_path, monkeypatch, selected_row, reuse):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(PocketDetectionWidget, "refresh", lambda self: None)
    app = QApplication.instance() or QApplication([])
    store = ProjectStore.open_at(tmp_path / "project.db")
    with store.get_session() as session:
        session.add_all([
            MoleculeRecord(id=4, name="first", is_receptor=True),
            MoleculeRecord(id=7, name="selected", is_receptor=True),
        ])
        session.commit()
    runtime = SimpleNamespace(
        workspace=SimpleNamespace(project_db=store),
        _require_active_project=lambda: None,
        list_jobs=lambda **kwargs: [],
    )
    runtime.binding_sites = BindingSiteAPI(runtime)
    runtime.binding_sites.predict = Mock(return_value="pockets")
    runtime.binding_sites.plan_prediction = Mock(return_value=P2RankPredictionPlan(
        receptor_ids=(4, 7),
        recalculate_receptor_ids=() if reuse else (4, 7),
        reused_receptor_ids=(4, 7) if reuse else (),
    ))
    host = QWidget()
    host.monitor_bridge = _Bridge([])
    table = SmartTableView(db=store, config=TableConfig(
        model_class=MoleculeRecord, columns=[ColumnDef("id"), ColumnDef("name")],
    ), parent=host)
    sites = BindingSitesWidget(runtime=runtime, parent=host)
    host.aux = SimpleNamespace(page_for=lambda _view: sites)
    tool = PocketDetectionWidget(runtime=runtime, parent=host)
    tool._receptors = {4: {"id": 4}, 7: {"id": 7}}
    tool._bind_receptor_table_signals(SimpleNamespace(table=table))
    layout = QVBoxLayout(host)
    for widget in (tool, table, sites):
        layout.addWidget(widget)
    host.show()
    app.processEvents()
    if selected_row is not None:
        table.view.selectRow(selected_row)

    def save_results():
        with store.get_session() as session:
            session.add_all([
                BindingSite(id=40, molecule_id=4, name="first pocket", source="p2rank", source_ref="1"),
                BindingSite(id=70, molecule_id=7, name="selected pocket", source="p2rank", source_ref="1"),
            ])
            session.commit()

    try:
        if reuse:
            save_results()
        tool._submit_prediction()
        if not reuse:
            save_results()
            host.monitor_bridge.job_finished.emit("pockets", "completed")
        deadline = time.monotonic() + 2
        while sites.table.rowCount() == 0 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert sites.table.rowCount() == 1
        expected_id = 4 if selected_row is None else 7
        assert sites._sites[0].molecule_id == expected_id
        assert table.get_selected_object().id == expected_id
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


def _count_refreshes(widget, monkeypatch):
    calls = []
    monkeypatch.setattr(PocketDetectionWidget, "refresh", lambda self: calls.append(self))
    return calls


def test_an_import_finishing_refreshes_the_open_tools_receptors(panel, monkeypatch):
    widget, bridge, _active, _api = panel
    refreshes = _count_refreshes(widget, monkeypatch)
    widget.parentWidget().show()
    QApplication.processEvents()
    refreshes.clear()  # showing it refreshes too; the import is what's under test

    # A receptor import is not this tool's job, but it changes what the tool can scope.
    bridge.job_finished.emit("import", "completed")
    assert refreshes == [widget]


def test_reopening_the_tool_refreshes_receptors_that_changed_while_it_was_hidden(panel, monkeypatch):
    widget, bridge, _active, _api = panel
    refreshes = _count_refreshes(widget, monkeypatch)
    bridge.job_finished.emit("import", "completed")
    assert refreshes == []  # hidden: nothing to show it on yet

    widget.parentWidget().show()
    QApplication.processEvents()
    assert refreshes == [widget]


def _scope_with_a_sequence_only_receptor(widget, api, *, runnable=True):
    from PySide6.QtWidgets import QMessageBox

    widget._receptors = {1: {"id": 1}, 2: {"id": 2}} if runnable else {2: {"id": 2}}
    api.plan_prediction.return_value = SimpleNamespace(
        recalculate_receptor_ids=(1,) if runnable else (), reused_receptor_ids=(),
        missing_structure_ids=(2,), missing_structure_names=("no_structure",),
    )
    return QMessageBox


@pytest.mark.parametrize("answer, submitted", [("Yes", True), ("No", False)])
def test_a_receptor_without_3d_is_named_and_skipped_only_if_the_user_agrees(panel, monkeypatch, answer, submitted):
    widget, _bridge, _active, api = panel
    box = _scope_with_a_sequence_only_receptor(widget, api)
    asked = []
    monkeypatch.setattr(box, "question", lambda _parent, _title, text, *a, **k: asked.append(text)
                        or getattr(box.StandardButton, answer))

    widget._submit_prediction()

    assert "no_structure" in asked[0] and "3D generation" in asked[0]
    if submitted:
        api.predict.assert_called_once()
        assert api.predict.call_args.kwargs["receptor_ids"] == [1]
        assert "1 without a 3D structure skipped" in widget.status_label.text()
    else:
        api.predict.assert_not_called()


def test_a_scope_of_only_sequence_receptors_explains_instead_of_failing_vaguely(panel, monkeypatch):
    widget, _bridge, _active, api = panel
    box = _scope_with_a_sequence_only_receptor(widget, api, runnable=False)
    shown = []
    monkeypatch.setattr(box, "information", lambda _parent, _title, text, *a, **k: shown.append(text))

    widget._submit_prediction()

    api.predict.assert_not_called()
    assert "no_structure" in shown[0] and "no 3D structure" in shown[0]
    assert "no_structure" in widget.status_label.text()

