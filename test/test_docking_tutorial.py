"""Hands-on tutorial transitions must follow successful work, not button clicks alone."""
import json
import shutil
import os
from threading import Event
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from amdockvs.runtime import AMDockVSRuntime
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDockWidget, QFileDialog, QFrame, QMessageBox, QTableWidgetItem, QWidget
from amdockvs.ui.docking_tutorial import copy_example_inputs, example_data, start_simple_docking_tutorial
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.shell.main_content import MainContentWidget
from amdockvs.ui.view_ids import DOCKING_VIEW_ID


def create_practice_project(runtime, parent):
    from ms_flow.api import ProjectCatalogBackend
    backend = ProjectCatalogBackend(app_id_filter=runtime.app_id, discover_apps=False,
                                    app_modules=["amdockvs.manifest"])
    folder = parent / "practice"
    if folder.exists():
        folder = parent / "another-practice"
    try:
        project = backend.create_project(name=folder.name, folder=folder, description="Test practice")
    finally:
        backend.shutdown()
    copy_example_inputs(project)
    return project


def wait_until(app, predicate):
    for _ in range(500):
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    pytest.fail("Tutorial did not reach the expected state")


def test_welcome_card_starts_a_tutorial_without_opening_the_project_browser():
    app = QApplication.instance() or QApplication([])
    content = MainContentWidget()
    content.resize(900, 700)
    content.show()
    app.processEvents()
    started, projects = [], []
    content.docking_tutorial_requested.connect(lambda: started.append(True))
    content.open_project_requested.connect(lambda: projects.append(True))
    content.docking_tutorial_button.click()
    assert started == [True] and projects == []
    card = content.findChild(QFrame, "simpleDockingTutorialCard")
    assert card.isVisible()
    assert content.docking_tutorial_button.isVisible()
    primary = content.findChild(QWidget, "welcomeProjectControls")
    assert card.geometry().bottom() > primary.geometry().bottom()
    assert card.geometry().left() < primary.geometry().left()
    assert not card.geometry().intersects(primary.geometry())
    content.close()


def test_bundled_example_is_usable_offline_and_the_box_contains_the_ligand():
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors

    example = example_data()
    metadata = json.loads((example / "example.json").read_text())
    receptor = (example / metadata["receptor"]).read_text().splitlines()
    assert sum(line.startswith("ATOM  ") for line in receptor) == metadata["receptor_atom_count"]
    assert not any(line.startswith("HETATM") for line in receptor)
    ligand = next(iter(Chem.SDMolSupplier(str(example / metadata["ligand"]), removeHs=False)))
    assert ligand is not None and ligand.GetNumHeavyAtoms() == 30
    assert rdMolDescriptors.CalcMolFormula(ligand) == "C19H23ClF3N5O2"
    assert any(bond.GetIsAromatic() for bond in ligand.GetBonds())
    assert any(bond.GetBondType() == Chem.BondType.DOUBLE for bond in ligand.GetBonds())
    xyz = ligand.GetConformer().GetPositions()
    for axis in range(3):
        assert all(abs(value - metadata["grid_center"][axis]) < metadata["grid_size"][axis] / 2
                   for value in xyz[:, axis])
    assert "C6" in metadata["source_caveat"]


@pytest.fixture
def practice(tmp_path, monkeypatch):
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = AMDockVSMainWindow(runtime=runtime)
    window.resize(1400, 900)
    from amdockvs.ui.tools.binding_sites.box import GridBoxSettingDockWidget
    grid_host = QDockWidget("Grid Box", window)
    window.grid_dock = GridBoxSettingDockWidget(runtime=runtime, parent=grid_host)
    grid_host.setWidget(window.grid_dock)
    window.addDockWidget(Qt.RightDockWidgetArea, grid_host)
    grid_host.hide()
    window.show()
    project = create_practice_project(runtime, tmp_path)
    assert runtime.active_context is None
    window._on_application_project_requested(str(project.id))
    app.processEvents()
    # These transition tests replace workers; the full real calculation is covered by
    # the opt-in e2e walkthrough. Scanning must not start an unrelated child process.
    from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

    def ready(panel, paths):
        panel._file_paths = list(paths)
        panel._scan_complete = True
        panel.table.setRowCount(len(paths))
        for row, path in enumerate(paths):
            panel.table.setItem(row, 0, QTableWidgetItem(Path(path).name))
        panel._sync_file_controls()
        panel._emit_ready(bool(paths))

    monkeypatch.setattr(ReceptorImportPanel, "set_files", ready)
    # The end-to-end test exercises the nested modal event loop; keep these
    # transition tests synchronous while clicking the real catalog action.
    monkeypatch.setattr(QDialog, "exec", lambda dialog: dialog.show())
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[1]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(str(args[1:3])))
    try:
        yield app, runtime, window, messages
    finally:
        window.close()
        runtime.shutdown()
        app.processEvents()


def submit_import(tutorial, role, jobs, monkeypatch):
    app = QApplication.instance()
    previous = tutorial.dialogs.get(role)
    assert previous is None or not previous.isVisible()
    if not tutorial.catalogs[role].isVisible():
        binding = tutorial.controller.targets[role]
        QTest.mouseClick(binding.widget(), Qt.LeftButton, pos=binding.rect().center())
        app.processEvents()
    wait_until(app, tutorial.import_buttons[role].isVisible)
    assert tutorial.import_buttons[role].objectName() == "EmptyActionButton"
    assert tutorial.import_buttons[role].text().rstrip("….") == f"Import {role.title()}s"
    assert tutorial.controller.targets[role].widget() is tutorial.import_buttons[role]
    # Closing the preceding modal dialog can temporarily leave no active window.
    monkeypatch.setattr(QApplication, "activeWindow", lambda: None)
    QTest.mouseClick(tutorial.import_buttons[role], Qt.LeftButton)
    app.processEvents()
    dialog = tutorial.dialogs[role]
    assert dialog is not previous
    assert dialog.isVisible()
    assert tutorial._import_paths(role) == []
    assert not dialog.import_button.isEnabled()
    add_button = tutorial.controller.targets[role].widget()
    assert add_button.text() == "Add files"
    # Cancelling the chooser must leave the dialog empty and keep this substep.
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args: ([], ""))
    QTest.mouseClick(add_button, Qt.LeftButton)
    assert tutorial._import_paths(role) == []
    chooser_calls = []
    def choose(*args):
        chooser_calls.append(args)
        return [str(tutorial.inputs / tutorial.example[role])], ""
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", choose)
    QTest.mouseClick(add_button, Qt.LeftButton)
    app.processEvents()
    assert chooser_calls[0][2] == str(tutorial.inputs)
    assert tutorial._import_paths(role) == [str(tutorial.inputs / tutorial.example[role])]
    assert tutorial.controller.targets[role].widget() is dialog.import_button
    monkeypatch.setattr(dialog, "workflow_submit", lambda **_: (lambda _rt: jobs, "Import"))
    monkeypatch.setattr(tutorial.runtime.loader, "duplicate_imports", lambda _: [])
    dialog.import_button.click()
    assert not dialog.isVisible()
    assert tutorial.controller.is_active
    assert tutorial.controller.overlay.parentWidget() is tutorial.window


def test_creation_keeps_an_open_project_and_uses_a_fresh_directory(practice, tmp_path):
    _app, runtime, _window, _messages = practice
    active = runtime.active_context
    another = create_practice_project(runtime, tmp_path)
    assert runtime.active_context is active
    assert Path(another.path) != Path(active.path)
    assert (Path(another.path) / "inputs" / "SAR405.sdf").is_file()


def test_starting_from_an_open_project_restarts_into_the_practice_project(practice, tmp_path, monkeypatch):
    from amdockvs import app as application
    from PySide6.QtWidgets import QFileDialog

    _app, runtime, window, _messages = practice
    active = runtime.active_context
    calls = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(tmp_path))
    monkeypatch.setattr(application, "restart", lambda *args: calls.append(args))
    window.central_widget.docking_tutorial_button.click()
    assert runtime.active_context is active
    assert len(calls) == 1
    assert calls[0] == (window, "--tutorial", "simple-docking")


def test_seven_steps_wait_for_successful_jobs_and_prepared_data(practice, monkeypatch):
    app, runtime, window, messages = practice
    tutorial = start_simple_docking_tutorial(window)
    assert tutorial.dialogs == {}  # starting the tutorial must not open an import dialog
    seen = [tutorial.controller.current_step.id]
    tutorial.controller.step_changed.connect(lambda step, _: seen.append(step))
    # Check stage readiness synchronously here; GUI/queued signal transitions stay real.
    import amdockvs.ui.docking_tutorial as module
    monkeypatch.setattr(module, "run_async", lambda fn, done, on_error=None: done(fn()))
    from amdockvs.models import MoleculeRecord

    def seed(role):
        with runtime.workspace.project_db.get_session() as session:
            session.add(MoleculeRecord(name=role, molecule_type="protein" if role == "receptor" else "small_molecule",
                                       is_receptor=role == "receptor", is_ligand=role == "ligand", n_atoms=30))
            session.commit()

    submit_import(tutorial, "receptor", ["rec-1", "rec-2"], monkeypatch)
    tutorial._job_finished("unrelated", "completed")
    tutorial._job_finished("rec-1", "completed")
    app.processEvents()
    assert tutorial.controller.index == 0
    seed("receptor")
    tutorial._job_finished("rec-2", "completed")
    wait_until(app, lambda: tutorial.controller.index == 1)
    assert "ligand" not in tutorial.dialogs  # the next load step also awaits a catalog click
    assert list(runtime.binding_sites.select()) == []  # the tutorial must not define the box

    # Going Back before submitting must close the modal dialog and return to
    # the catalog; visiting the load step again still requires the user's click.
    binding = tutorial.controller.targets["ligand"]
    assert binding.widget() is window.central_widget.main_content_tabs.tabBar()
    QTest.mouseClick(binding.widget(), Qt.LeftButton, pos=binding.rect().center())
    app.processEvents()
    QTest.mouseClick(tutorial.import_buttons["ligand"], Qt.LeftButton)
    app.processEvents()
    unsubmitted = tutorial.dialogs["ligand"]
    assert unsubmitted.isVisible()
    tutorial.controller.back()
    assert tutorial.controller.index == 0 and not unsubmitted.isVisible()
    tutorial.controller.next()
    assert tutorial.controller.index == 1 and not unsubmitted.isVisible()

    submit_import(tutorial, "ligand", ["lig-1"], monkeypatch)
    seed("ligand")
    tutorial._job_finished("lig-1", "completed")
    wait_until(app, lambda: tutorial.controller.index == 2)
    window.tools.action_buttons[DOCKING_VIEW_ID].click()
    wait_until(app, lambda: tutorial.controller.index == 3)
    # The user configures the program through the actual controls.
    for key, check in tutorial.studio._program_checks.items():
        if check.isChecked() != (key == "vina"):
            check.click()
    tutorial.studio.stepper.next()
    wait_until(app, lambda: tutorial.controller.index == 4)
    tutorial.studio.ligand_prep_jobs.finished.emit("failed")
    app.processEvents()
    assert tutorial.controller.index == 4
    ready = {"total": 1, "ready": 0, "failed": 0}
    monkeypatch.setattr(runtime.docking, "preparation_summary", lambda *a, **k: dict(ready))
    tutorial.studio.ligand_prep_jobs.finished.emit("completed")
    app.processEvents()
    assert tutorial.controller.index == 4  # successful job without prepared data is insufficient
    ready["ready"] = 1
    tutorial.studio.ligand_prep_jobs.finished.emit("completed")
    wait_until(app, lambda: tutorial.controller.index == 5)
    assert tutorial.studio.stepper.current_index == 1  # no tutorial-driven navigation
    assert not tutorial._grid_ready
    grid = window.grid_dock
    grid.set_molecule(runtime.molecules.select(role="receptor").one())
    grid.new_button.click()
    for spin, value in zip((grid.cx, grid.cy, grid.cz, grid.sx, grid.sy, grid.sz),
                           (*tutorial.example["grid_center"], *tutorial.example["grid_size"])):
        spin.setValue(value)
    grid.save_button.click()
    assert not tutorial._grid_ready
    grid.activate_button.click()
    wait_until(app, lambda: tutorial._grid_ready)
    tutorial.studio.receptor_prep_jobs.finished.emit("completed")
    wait_until(app, lambda: tutorial.controller.index == 6)
    tutorial._track("docking", ["dock-1"])
    monkeypatch.setattr(runtime.docking, "result_stats", lambda: SimpleNamespace(completed_results=1))
    tutorial._job_finished("dock-1", "completed")
    wait_until(app, lambda: not tutorial.controller.is_active)
    assert messages == ["Tutorial completed"]
    assert seen == ["load-receptor", "load-ligand", "load-receptor", "load-ligand",
                    "docking-studio", "programs", "prepare-ligand",
                    "prepare-receptor", "run-docking"]


def test_cancel_disconnects_and_ignores_late_job_submission(practice, monkeypatch):
    app, _runtime, window, messages = practice
    tutorial = start_simple_docking_tutorial(window)
    submit_import(tutorial, "receptor", ["rec"], monkeypatch)
    tutorial._job_finished("rec", "completed_with_errors")
    app.processEvents()
    assert tutorial.controller.index == 0
    assert tutorial.dialogs["receptor"].isVisible()
    tutorial.controller.stop()
    tutorial._track("receptor", ["late-job"])
    tutorial._job_finished("late-job", "completed")
    assert not tutorial.controller.is_active and tutorial.pending == {}
    assert not tutorial.dialogs["receptor"].isVisible()
    assert messages == []


def test_import_finishing_while_on_the_previous_step_is_not_lost(practice, monkeypatch):
    app, runtime, window, _messages = practice
    tutorial = start_simple_docking_tutorial(window)
    import amdockvs.ui.docking_tutorial as module
    from amdockvs.models import MoleculeRecord

    monkeypatch.setattr(module, "run_async", lambda fn, done, on_error=None: done(fn()))
    submit_import(tutorial, "receptor", ["rec"], monkeypatch)
    with runtime.workspace.project_db.get_session() as session:
        session.add(MoleculeRecord(name="receptor", molecule_type="protein", is_receptor=True, n_atoms=30))
        session.commit()
    tutorial._job_finished("rec", "completed")
    wait_until(app, lambda: tutorial.controller.current_step.id == "load-ligand")
    submit_import(tutorial, "ligand", ["lig"], monkeypatch)
    tutorial.controller.back()
    with runtime.workspace.project_db.get_session() as session:
        session.add(MoleculeRecord(name="ligand", molecule_type="small_molecule", is_ligand=True, n_atoms=30))
        session.commit()
    tutorial._job_finished("lig", "completed")
    assert tutorial.controller.current_step.id == "load-receptor"
    tutorial.controller.next()
    wait_until(app, lambda: tutorial.controller.current_step.id == "docking-studio")
    assert tutorial.pending == {} and "ligand" in tutorial.completed_stages


def test_cancelling_a_slow_preview_keeps_the_ui_responsive_and_defers_window_close(practice, monkeypatch):
    app, _runtime, window, _messages = practice
    release = Event()

    class Preview(QThread):
        def run(self):
            release.wait(5)

    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    tutorial = start_simple_docking_tutorial(window)
    wait_until(app, tutorial.import_buttons["receptor"].isVisible)
    tutorial.import_buttons["receptor"].click()
    dialog = tutorial.dialogs["receptor"]
    panel = dialog.panel
    panel.set_files([str(tutorial.inputs / tutorial.example["receptor"])])
    thread = Preview(panel)
    thread.finished.connect(panel._on_preview_thread_finished)
    thread.start()
    try:
        wait_until(app, lambda: panel.preview_running)
        tutorial.controller.stop()
        assert not tutorial.controller.is_active and panel.preview_running
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        app.processEvents()
        assert heartbeat == [True]
        window.close()
        assert not window._closing and window.isVisible()
        release.set()
        wait_until(app, lambda: window._closing and not window.isVisible())
    finally:
        release.set()
        wait_until(app, lambda: not panel.preview_running)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)


@pytest.mark.parametrize("role", ["receptor", "ligand"])
def test_identical_copies_are_accepted_and_wrong_contents_can_be_removed(practice, tmp_path, monkeypatch, role):
    app, _runtime, window, _messages = practice
    tutorial = start_simple_docking_tutorial(window)
    if role == "ligand":
        tutorial.receptor_loaded.emit()
        app.processEvents()
    binding = tutorial.controller.targets[role]
    if not tutorial.catalogs[role].isVisible():
        QTest.mouseClick(binding.widget(), Qt.LeftButton, pos=binding.rect().center())
        app.processEvents()
    wait_until(app, tutorial.import_buttons[role].isVisible)
    tutorial.import_buttons[role].click()
    dialog = tutorial.dialogs[role]
    copy = tmp_path / tutorial.example[role]
    shutil.copyfile(tutorial.inputs / tutorial.example[role], copy)
    set_files = dialog.panel.set_files if role == "receptor" else dialog.table.add_files
    set_files([str(copy)])
    app.processEvents()
    assert tutorial._correct_import(role)
    assert dialog.import_button.isEnabled()
    tutorial.controller.refresh()
    assert tutorial._import_target(role) is dialog.import_button
    assert not tutorial.controller.overlay.panel.geometry().intersects(tutorial.controller.overlay.hole)
    # The name is identical but the contents are wrong. This must remain blocked.
    copy.write_text("wrong contents")
    tutorial._validate_import_choice(role)
    assert not dialog.import_button.isEnabled()
    table = dialog.panel.table if role == "receptor" else dialog.table
    table.clearSelection()
    tutorial.controller.refresh()
    assert tutorial._import_target(role) is table
    assert not tutorial.controller.overlay.panel.geometry().intersects(tutorial.controller.overlay.hole)
    QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(table.model().index(0, 0)).center())
    app.processEvents()
    remove = tutorial._import_target(role)
    assert remove.text() == "Remove"
    QTest.mouseClick(remove, Qt.LeftButton)
    app.processEvents()
    assert tutorial._import_paths(role) == []
    tutorial.controller.stop()


def test_welcome_tutorial_waits_for_user_project_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = AMDockVSMainWindow(runtime=runtime)
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    try:
        window.central_widget.docking_tutorial_button.click()
        tutorial = window._docking_tutorial
        assert runtime.active_context is None
        assert runtime.list_projects(page=1, items_per_page=100) == []
        assert tutorial.controller.current_step.id == "create-project"
        assert tutorial._project_target() is window.central_widget.open_project_button
        window.central_widget.open_project_button.click()
        app.processEvents()
        browser = window._app_widget._projects_widget.project_browser
        assert tutorial._project_target() is browser.new_button
        assert runtime.active_context is None
        monkeypatch.setattr(QDialog, "exec", lambda dialog: dialog.show())
        browser.new_button.click()
        app.processEvents()
        form = tutorial._project_form
        assert tutorial._project_target() is form.name_input
        form.name_input.setText("Practice")
        form.name_input.editingFinished.emit()
        assert tutorial._project_target() is form.path_row
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: "")
        form.browse_button.click()
        assert not tutorial._project_path_chosen
        assert tutorial._project_target() is form.path_row
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
        form.browse_button.click()
        assert tutorial._project_path_chosen
        assert tutorial._project_target() is form.save_button
        tutorial.controller.stop()
        assert runtime.active_context is None
    finally:
        window.close()
        runtime.shutdown()
        app.processEvents()
