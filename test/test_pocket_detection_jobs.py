"""Pocket Detection follows P2Rank work continued through the Jobs monitor."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6
from amdockvs.binding_sites.jobs import p2rank_prediction_job
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget
from ms_components.ms_monitor.models import JobMonitorState, ProjectMonitorState
from amdockvs.ui.tools.binding_sites.detection import PocketDetectionWidget


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
        plan_prediction=Mock(return_value=SimpleNamespace(recalculate_receptor_ids=(1,), reused_receptor_ids=())),
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
