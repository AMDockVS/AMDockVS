"""A completed build updates the displayed catalog structure without changing tabs."""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pytest
import pymol2
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from amdockvs.models import MoleculeRecord
from amdockvs.ui.catalog.molecules import MoleculeWidget
from amdockvs.ui.catalog.receptors import ReceptorWidget
from amdockvs.ui.shell.job_feedback import JobFeedbackController
from amdockvs.ui.shell.view_coordinator import ViewCoordinator
from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
from payload_seed import seed_payload


PDB = (
    "ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00 90.00           N\n"
    "ATOM      2  CA  GLY A   1       1.400   0.000   0.000  1.00 90.00           C\n"
    "ATOM      3  C   GLY A   1       2.000   1.200   0.000  1.00 90.00           C\nEND\n"
)


def _wait(app, condition):
    deadline = time.monotonic() + 10
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


@pytest.fixture(params=[ReceptorWidget, MoleculeWidget])
def catalog_scene(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = QWidget()
    view = None
    try:
        runtime.create_project(name="refresh", folder=tmp_path / "project", description="")
        with runtime.workspace.project_db.get_session() as session:
            session.add_all([
                MoleculeRecord(name=name, molecule_type="protein", is_receptor=True)
                for name in ("other", "sequence")
            ])
            session.commit()
        with pymol2.PyMOL() as pymol:
            window.runtime = runtime
            window.pymol_dock = SimpleNamespace(cmd=pymol.cmd, show=lambda: None)
            window.grid_dock = None
            window.aux = SimpleNamespace(show_catalog_selection_details=lambda *a: None, refresh=lambda: None)
            window.viewer = MolecularViewerController(window)
            view = request.param(runtime=runtime, parent=window)
            layout = QVBoxLayout(window)
            layout.addWidget(view)
            window.central_widget = SimpleNamespace(
                _open_tabs={"catalog": view}, refresh_open_view=lambda _id: view.refresh(),
            )
            window.views = ViewCoordinator(window)
            monkeypatch.setattr(window.views, "sync_catalog_availability", lambda: None)
            window.diagram_dock = SimpleNamespace(reload=lambda: None)
            feedback = JobFeedbackController.__new__(JobFeedbackController)
            feedback.w = window
            feedback._report_import_summary = lambda: None
            feedback._running_jobs = set()
            window.show()
            app.processEvents()
            view.table.view.selectRow(1)
            selected = view.table.get_selected_object()
            window.viewer.select_table_row(view.table.view)
            view._load_object_in_pymol(selected)
            app.processEvents()
            assert pymol.cmd.count_atoms() == 0

            def predict():
                payload = seed_payload(runtime.workspace.project_db, PDB, "pdb")
                with runtime.workspace.project_db.get_session() as session:
                    molecule = session.get(MoleculeRecord, selected.id)
                    molecule.current_payload_id = payload
                    molecule.has_3d = True
                    session.commit()
                feedback.on_job_finished("prediction", "completed")
                return payload

            try:
                yield app, window, view, selected.id, predict
            finally:
                window.viewer.clear_scene()
                window.pymol_dock.cmd = None
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
        runtime.shutdown()


def test_completed_prediction_shows_selected_sequence_without_switching_tabs(catalog_scene):
    app, window, view, molecule_id, predict = catalog_scene
    payload = predict()
    _wait(app, lambda: window.pymol_dock.cmd.count_atoms() == 3)
    selected = view.table.get_selected_object()
    assert selected.id == molecule_id
    assert selected.current_payload_id == payload
    prefix = "receptor" if isinstance(view, ReceptorWidget) else "molecule"
    assert window.pymol_dock.cmd.get_names("objects") == [f"{prefix}_{molecule_id}"]


def test_completed_prediction_does_not_replace_another_views_scene(catalog_scene):
    app, window, view, molecule_id, predict = catalog_scene
    window.viewer.select_table_row(object())
    predict()
    for _ in range(5):
        app.processEvents()
    assert window.pymol_dock.cmd.count_atoms() == 0


def test_unrelated_job_keeps_the_existing_structure_and_camera(catalog_scene):
    app, window, view, molecule_id, predict = catalog_scene
    predict()
    _wait(app, lambda: window.pymol_dock.cmd.count_atoms() == 3)
    cmd = window.pymol_dock.cmd
    cmd.turn("y", 45)
    camera = cmd.get_view()
    request = window.viewer._request
    view.refresh()
    for _ in range(5):
        app.processEvents()
    assert cmd.get_view() == camera
    assert window.viewer._request == request
    assert view.table.get_selected_object().id == molecule_id


def test_predicting_again_replaces_the_displayed_model(catalog_scene):
    app, window, view, molecule_id, predict = catalog_scene
    first = predict()
    _wait(app, lambda: first in window.viewer._loaded.values())
    second = predict()
    _wait(app, lambda: second in window.viewer._loaded.values())
    assert first != second
    assert window.pymol_dock.cmd.count_atoms() == 3
    assert view.table.get_selected_object().id == molecule_id


def test_completed_prediction_keeps_an_accumulated_scene(catalog_scene):
    app, window, view, molecule_id, predict = catalog_scene
    first = predict()
    _wait(app, lambda: first in window.viewer._loaded.values())
    window.pymol_dock.cmd.pseudoatom("comparison")
    window.pymol_dock._amdock_accumulated = True
    predict()
    for _ in range(5):
        app.processEvents()
    assert "comparison" in window.pymol_dock.cmd.get_names("objects")
    assert first in window.viewer._loaded.values()
    assert view.table.get_selected_object().id == molecule_id
