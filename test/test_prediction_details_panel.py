"""Build prediction reveals pLDDT details only after its new structure reaches PyMOL."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pymol2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import BUILD_ID, MOLECULES_VIEW_ID, RECEPTOR_VIEW_ID
from payload_seed import seed_payload
from test_catalog_structure_refresh import PDB, _wait


@pytest.fixture
def prediction_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    monkeypatch.setenv("ESM_API_KEY", "test-token")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = None
    try:
        runtime.create_project(name="prediction_details", folder=tmp_path / "project", description="")
        store = runtime.workspace.project_db
        old_payload = seed_payload(store, PDB, "pdb")
        with store.get_session() as session:
            sequence = MoleculeRecord(name="sequence", molecule_type="protein", is_receptor=True)
            existing = MoleculeRecord(name="existing", molecule_type="protein", is_receptor=True,
                                      current_payload_id=old_payload, has_3d=True)
            session.add_all([sequence, existing])
            session.commit()
            session.refresh(sequence)
            session.refresh(existing)
            sequence_id, existing_id = sequence.id, existing.id
        window = AMDockVSMainWindow(runtime=runtime)
        window.resize(1600, 1000)
        window.show()
        with pymol2.PyMOL() as session:
            window.pymol_dock = SimpleNamespace(cmd=session.cmd, show=lambda: None,
                                               set_side_panel_visible=lambda _visible: None)
            monkeypatch.setattr(window.charts, "sync_to_active_view", lambda _view_id: None)
            build = window.tools.open_tool(BUILD_ID)
            build.tabs.setCurrentWidget(build.proteins_tab)
            for section in build._protein_sections():
                section.setChecked(False)
            build.receptor_predict_section.setChecked(True)
            app.processEvents()
            view = window.central_widget.open_view(MOLECULES_VIEW_ID)
            view.table.view.selectRow(0)
            window.viewer.select_table_row(view.table.view)
            view._load_object_in_pymol(view.table.get_selected_object())
            window.aux.action.setChecked(False)
            window.aux.on_toggled(False)
            monkeypatch.setattr(runtime.chemistry, "predict_receptors", lambda **kwargs: "prediction")
            build._run_proteins()
            assert build.protein_jobs.job_ids == ["prediction"]
            assert not window.central_widget.aux_widget.isVisible()

            def complete(status="completed", *, model=True):
                payload = None
                if model:
                    payload = seed_payload(store, PDB, "pdb")
                    with store.get_session() as db:
                        molecule = db.get(MoleculeRecord, sequence_id)
                        molecule.current_payload_id = payload
                        molecule.has_3d = True
                        db.add(MoleculeModel(molecule_id=sequence_id, payload_id=payload, source="esmfold"))
                        db.commit()
                window.monitor_bridge.job_finished.emit("prediction", status)
                return payload

            try:
                yield SimpleNamespace(app=app, window=window, build=build, view=view, complete=complete,
                                      sequence_id=sequence_id, existing_id=existing_id, runtime=runtime)
            finally:
                # Invalidate queued reads before the native PyMOL session is destroyed.
                window.viewer.clear_scene()
                window.pymol_dock.cmd = None
                window.pymol_dock = None
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
        app.processEvents()


def test_prediction_opens_details_with_plddt_after_the_structure_is_shown(prediction_scene):
    scene = prediction_scene
    payload = scene.complete()
    _wait(scene.app, lambda: scene.window.central_widget.aux_widget.isVisible())
    assert scene.window.viewer._already_shown(f"molecule_{scene.sequence_id}", payload)
    details = scene.window.aux.views[scene.window.aux.AUX_DETAILS]
    assert details._current_molecule.id == scene.sequence_id
    assert details.show_plddt_button.isEnabled() and details.plddt_legend.isVisible()
    details.grab().save("/tmp/amdockvs-prediction-details.png")

    scene.window.aux.action.setChecked(False)
    scene.window.aux.on_toggled(False)
    scene.view._load_object_in_pymol(scene.runtime.molecules.get(scene.sequence_id))
    scene.app.processEvents()
    assert not scene.window.central_widget.aux_widget.isVisible()  # A later click doesn't reopen it.


def test_prediction_details_buttons_load_structures_and_color_the_displayed_atoms(prediction_scene):
    scene = prediction_scene
    scene.window.open_or_focus_view(RECEPTOR_VIEW_ID)
    scene.app.processEvents()
    scene.complete()
    _wait(scene.app, lambda: scene.window.central_widget.aux_widget.isVisible())
    details = scene.window.aux.views[scene.window.aux.AUX_DETAILS]
    cmd = scene.window.pymol_dock.cmd
    name = f"receptor_{scene.sequence_id}"
    assert cmd.get_names("objects") == [name]
    QTest.mouseClick(details.show_plddt_button, Qt.LeftButton)
    _wait(scene.app, lambda: cmd.get_color_index("plddt_confident") >= 0)
    colors = []
    cmd.iterate(name, "colors.append(color)", space={"colors": colors})
    assert set(colors) == {cmd.get_color_index("plddt_confident")}
    assert cmd.get_names("objects") == [name]
    for button in (details.show_primary_button, details.show_secondary_button):
        scene.window.viewer.clear_scene()
        QTest.mouseClick(button, Qt.LeftButton)
        _wait(scene.app, lambda: cmd.count_atoms() == 3)


def test_current_and_stored_buttons_replace_the_same_receptor_with_the_requested_coordinates(prediction_scene):
    scene = prediction_scene
    scene.window.open_or_focus_view(RECEPTOR_VIEW_ID)
    scene.app.processEvents()
    current = scene.complete()
    _wait(scene.app, lambda: scene.window.central_widget.aux_widget.isVisible())
    store = scene.runtime.workspace.project_db
    stored = seed_payload(store, PDB.replace("   0.000   0.000   0.000", "   9.000   0.000   0.000"), "pdb")
    with store.get_session() as session:
        molecule = session.get(MoleculeRecord, scene.sequence_id)
        molecule.stored_payload_id = stored
        session.commit()
    scene.window.aux.show_catalog_selection_details("receptor", scene.runtime.molecules.get(scene.sequence_id))
    details = scene.window.aux.views[scene.window.aux.AUX_DETAILS]
    cmd, viewer = scene.window.pymol_dock.cmd, scene.window.viewer
    name = f"receptor_{scene.sequence_id}"
    for button, payload, x in ((details.show_secondary_button, stored, 9.0),
                               (details.show_primary_button, current, 0.0)):
        QTest.mouseClick(button, Qt.LeftButton)
        _wait(scene.app, lambda: viewer._loaded.get(name) == payload)
        assert cmd.get_names("objects") == [name]
        assert cmd.get_model(name).atom[0].coord[0] == pytest.approx(x)


def test_prediction_waits_for_the_structure_read_before_opening_details(prediction_scene, monkeypatch):
    scene = prediction_scene
    read = scene.window.viewer._read
    pending = []
    monkeypatch.setattr(scene.window.viewer, "_read", lambda wanted, then: pending.append((wanted, then)))
    scene.complete()
    _wait(scene.app, lambda: bool(pending))
    assert not scene.window.central_widget.aux_widget.isVisible()
    for wanted, then in pending:
        read(wanted, then)
    _wait(scene.app, lambda: scene.window.central_widget.aux_widget.isVisible())


def test_a_build_without_prediction_does_not_open_details(prediction_scene, monkeypatch):
    scene = prediction_scene
    scene.build.receptor_predict_section.setChecked(False)
    scene.build.receptor_fix_section.setChecked(True)
    monkeypatch.setattr(scene.build, "_protein_tools_ready", lambda _ops: True)
    monkeypatch.setattr(scene.runtime.chemistry, "fix_receptors", lambda **kwargs: "fix-only")
    scene.build._run_proteins()
    payload = scene.complete()
    scene.window.monitor_bridge.job_finished.emit("fix-only", "completed")
    _wait(scene.app, lambda: payload in scene.window.viewer._loaded.values())
    assert not scene.window.central_widget.aux_widget.isVisible()


@pytest.mark.parametrize("status", ["failed", "canceled", "completed_with_errors"])
def test_failed_or_cancelled_prediction_does_not_open_details(prediction_scene, status):
    scene = prediction_scene
    # A partially successful batch can have a model even when the prediction job failed.
    payload = scene.complete(status)
    _wait(scene.app, lambda: payload in scene.window.viewer._loaded.values())
    assert not scene.window.central_widget.aux_widget.isVisible()


def test_no_new_prediction_does_not_open_details(prediction_scene):
    scene = prediction_scene
    scene.complete(model=False)
    for _ in range(5):
        scene.app.processEvents()
    assert not scene.window.central_widget.aux_widget.isVisible()


def test_prediction_does_not_open_details_after_leaving_build(prediction_scene):
    scene = prediction_scene
    scene.build.hide()
    scene.complete()
    for _ in range(5):
        scene.app.processEvents()
    assert not scene.window.central_widget.aux_widget.isVisible()


def test_an_existing_structure_in_the_prediction_scope_does_not_open_details(prediction_scene):
    scene = prediction_scene
    scene.view.table.view.selectRow(1)
    scene.view._load_object_in_pymol(scene.runtime.molecules.get(scene.existing_id))
    _wait(scene.app, lambda: scene.window.pymol_dock.cmd.count_atoms() == 3)
    scene.complete()
    for _ in range(5):
        scene.app.processEvents()
    assert not scene.window.central_widget.aux_widget.isVisible()
