"""The first hands-on tutorial: real controls, real jobs, and a separate practice project."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
import shutil

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QPushButton
from ms_components.ms_tutorial import Tutorial, TutorialController, TutorialEvent, TutorialStep, TutorialTarget

from amdockvs.core.jobs import STATUS
from amdockvs.ui.common.async_query import run_async
from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, DOCKING_VIEW_ID, LIGANDS_VIEW_ID, RECEPTOR_VIEW_ID


def example_data():
    return files("amdockvs") / "data" / "tutorials" / "simple_docking"


def copy_example_inputs(project):
    """Stage offline examples only after the user creates their own project."""
    inputs = Path(project.path) / "inputs"
    inputs.mkdir(exist_ok=True)
    for name in ("Vps34_receptor.pdb", "SAR405.sdf", "example.json", "README.md"):
        destination = inputs / name
        if destination.exists():
            if destination.read_bytes() != (example_data() / name).read_bytes():
                raise ValueError(f"The tutorial input already exists with different contents: {destination}")
            continue
        with (example_data() / name).open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
    return inputs


def launch_simple_docking_tutorial(window):
    if window.runtime.active_context is not None:
        from amdockvs.app import restart

        restart(window, "--tutorial", "simple-docking")
        return
    start_simple_docking_tutorial(window)


def start_simple_docking_tutorial(window):
    previous = getattr(window, "_docking_tutorial", None)
    if previous is not None:
        previous.controller.stop()
    overview = getattr(window, "_interface_tutorial", None)
    if overview is not None:
        overview.stop()
    try:
        tutorial = SimpleDockingTutorial(window)
        window._docking_tutorial = tutorial
        tutorial.start()
        return tutorial
    except Exception as exc:
        QMessageBox.warning(window, "Simple docking tutorial", str(exc))
        return None


class SimpleDockingTutorial(QObject):
    project_created = Signal()
    receptor_loaded = Signal()
    ligand_loaded = Signal()
    ligand_prepared = Signal()
    receptor_prepared = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.runtime = window.runtime
        self.inputs = None
        self.example = json.loads((example_data() / "example.json").read_text())
        if self.runtime.active_context is not None:
            if self.runtime.molecules.count(self.runtime.molecules.select()) != 0:
                raise ValueError("Start a new tutorial from the welcome-page card.")
            self.inputs = copy_example_inputs(self.runtime.active_context)
        self._created_project_id = None
        self._project_browser = None
        self._project_form = None
        self._project_name_entered = False
        self._file_hashes = {}
        self._expected_hashes = {role: hashlib.sha256((example_data() / self.example[role]).read_bytes()).digest()
                                 for role in ("receptor", "ligand")}
        self.dialogs = {}
        self.import_buttons = {}
        self.catalogs = {}
        self.studio = None
        self.pending = {}
        self.outcomes = {}
        self.completed_stages = set()
        self._checking = set()
        self._generation = 0
        self._feedback = ""
        self._grid_ready = False
        self._grid_checking = False
        self._grid_recheck = False
        self.controller = TutorialController(targets={
            "project": TutorialTarget(self._project_target),
            "receptor": TutorialTarget(lambda: self._import_target("receptor"), prepare=lambda: self._bind_import_catalog("receptor"), rect=lambda: self._import_rect("receptor")),
            "ligand": TutorialTarget(lambda: self._import_target("ligand"), prepare=lambda: self._bind_import_catalog("ligand"), rect=lambda: self._import_rect("ligand")),
            "studio": TutorialTarget(window.tools.action_buttons[DOCKING_VIEW_ID]),
            "programs": TutorialTarget(lambda: self.studio, prepare=self._bind_studio),
            "prepare_ligand": TutorialTarget(lambda: self.studio, prepare=self._bind_studio),
            "prepare_receptor": TutorialTarget(self._receptor_target, prepare=self._bind_studio),
            "run": TutorialTarget(self._run_target, prepare=self._bind_studio),
        }, events={
            "project_created": TutorialEvent(self.project_created, is_complete=lambda: self.inputs is not None),
            "receptor_loaded": TutorialEvent(self.receptor_loaded, is_complete=lambda: "receptor" in self.completed_stages),
            "ligand_loaded": TutorialEvent(self.ligand_loaded, is_complete=lambda: "ligand" in self.completed_stages),
            "studio_opened": TutorialEvent(window.tools.action_buttons[DOCKING_VIEW_ID].clicked),
            "programs_selected": TutorialEvent(lambda: self.studio.stepper.step_changed,
                accept=lambda *_: self.studio.stepper.current_index == 1
                    and self.studio._selected_programs() == ["vina"]
                    and self.studio._run_kind() == "docking" and self.studio._method() == "standard"
                    and self.studio._ligand_type() == "small_molecule"),
            "ligand_prepared": TutorialEvent(self.ligand_prepared, is_complete=lambda: "prepare_ligand" in self.completed_stages),
            "receptor_prepared": TutorialEvent(self.receptor_prepared, is_complete=lambda: "prepare_receptor" in self.completed_stages),
            "docking_started": TutorialEvent(lambda: self.studio.docking_submission_started),
        }, parent=self, instruction_provider=self._instruction, dim_background=False, close_only=True,
           click_only_close=True, block_tab=True)
        self.controller.step_changed.connect(lambda *_: setattr(self, "_feedback", ""))
        self.controller.finished.connect(self._finish)
        self.controller.cancelled.connect(self._cleanup)
        self.controller.failed.connect(self._failed)
        window.monitor_bridge.job_finished.connect(self._job_finished)
        window.import_dialog_opened.connect(self._bind_import_dialog)
        window._app_widget.projects_opened.connect(self._bind_projects)
        window.project_opened.connect(self._project_opened)

    def start(self):
        project_steps = () if self.inputs is not None else (
            TutorialStep("create-project", "project", "1. Create a project",
                "Click Open or Create Project, then New project. Give your practice project a name, "
                "then click Save to use the default project folder.", "project_created"),
        )
        steps = (
            TutorialStep("load-receptor", "receptor", "1. Load receptor",
                "Click Import Receptors in the middle of the Receptors page. "
                "Then add and select the bundled receptor file yourself.", "receptor_loaded"),
            TutorialStep("load-ligand", "ligand", "2. Load ligand",
                "Click Import Ligands in the middle of the Ligands page. "
                "Then add and select the bundled ligand file yourself.", "ligand_loaded"),
            TutorialStep("docking-studio", "studio", "3. Docking Studio",
                "Click Docking Studio on the left toolbar. This is where you choose a program, "
                "prepare your molecules and launch the calculation.", "studio_opened"),
            TutorialStep("programs", "programs", "4. Programs",
                "Use Docking with a Small molecule and AutoDock Vina for this example. "
                "Keep the default Vina settings, then click Next in the Programs panel to continue to Ligands.", "programs_selected"),
            TutorialStep("prepare-ligand", "prepare_ligand", "5. Prepare ligand",
                "Click Prepare Ligands to convert SAR405 into the format Vina needs. "
                "Keep the preparation defaults. The tutorial waits for successful preparation.", "ligand_prepared"),
            TutorialStep("prepare-receptor", "prepare_receptor", "6. Prepare receptor",
                "Open Receptors, define and activate the docking box, then click Prepare Receptors. "
                "Keep a rigid receptor. Wait for preparation to finish.", "receptor_prepared"),
            TutorialStep("run-docking", "run", "7. Run docking",
                "Review Preview & Run: one prepared ligand and one prepared receptor. "
                "Once the checks say Ready to run, click Run Docking. "
                "The tutorial ends when submission starts; poses and scores appear in Docking Results when the calculation finishes.", "docking_started"),
        )
        if project_steps:
            from dataclasses import replace
            steps = tuple(replace(step, title=f"{index + 2}. {step.title.split('. ', 1)[1]}")
                          for index, step in enumerate(steps))
        self.controller.start(Tutorial("simple-docking", "Simple docking · Vps34 and SAR405", project_steps + steps))

    def _bind_projects(self, dialog):
        if not self.controller.is_active or self.controller.current_step.id != "create-project":
            return
        browser = dialog.project_browser
        if browser is not self._project_browser:
            self._project_browser = browser
            browser.project_created.connect(self._remember_created_project)
            browser.project_form_opened.connect(self._bind_project_form)
        self.controller.refresh()

    def _bind_project_form(self, form):
        if not self.controller.is_active or form.mode != "create":
            return
        self._project_form = form
        self._project_name_entered = False
        form.name_input.installEventFilter(self)
        form.finished.connect(self.controller.refresh)
        QTimer.singleShot(0, self.controller.refresh)

    def _remember_created_project(self, project_id):
        if self.controller.is_active:
            self._created_project_id = str(project_id)

    def _project_opened(self, project_id):
        if not self.controller.is_active or self.controller.current_step.id != "create-project":
            return
        if str(project_id) != self._created_project_id:
            self.controller.stop()
            QMessageBox.information(self.window, "Simple docking tutorial",
                "Start the tutorial again and create a new practice project to keep its examples separate.")
            return
        try:
            self.inputs = copy_example_inputs(self.runtime.active_context)
        except Exception as exc:
            self._failed("simple-docking", str(exc))
            self.controller.stop()
            return
        self.project_created.emit()

    def _project_target(self):
        from shiboken6 import isValid
        if self.inputs is not None:
            return self.window.central_widget.main_content_tabs.tabBar()
        browser = self._project_browser
        if browser is not None and isValid(browser):
            form = self._project_form
            if form is not None and isValid(form) and form.isVisible():
                if not self._project_name_entered or not form.name_input.text().strip():
                    return form.name_input
                return form.save_button
            if browser.isVisible():
                return browser.new_button
        return self.window.central_widget.open_project_button

    def _project_name_finished(self, name):
        self._project_name_entered = bool(name)

    def eventFilter(self, watched, event):
        form = self._project_form
        if (form is not None and watched is form.name_input and self.controller.is_active
                and event.type() == QEvent.Type.KeyPress):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                name = form.name_input.text().strip()
                if name:
                    self._project_name_finished(name)
                    self.controller.refresh()
                    if self.controller.is_active:
                        form.save_button.setFocus()
                return True
        return super().eventFilter(watched, event)

    def _import_target(self, role):
        dialog = self.dialogs.get(role)
        if role in self.pending:
            return self.window.central_widget
        if dialog is not None and dialog.isVisible():
            paths = self._import_paths(role)
            if not paths:
                action = dialog.panel.add_files_action if role == "receptor" else dialog.add_action
                return action.parent().widgetForAction(action)
            if not self._correct_import(role):
                table = dialog.panel.table if role == "receptor" else dialog.table
                if not table.selectionModel().selectedRows():
                    return table
                action = dialog.panel.remove_files_action if role == "receptor" else dialog.remove_action
                return action.parent().widgetForAction(action)
            return dialog.import_button
        if self.outcomes.get(role):
            return self.window.central_widget
        if not self.catalogs[role].isVisible():
            return self.window.central_widget.main_content_tabs.tabBar()
        button = self.import_buttons[role]
        # The empty-state button appears after the catalog's first async query.
        return button if button.isVisible() else self.window.central_widget

    def _import_rect(self, role):
        widget = self._import_target(role)
        tabs = self.window.central_widget.main_content_tabs
        if widget is tabs.tabBar():
            return tabs.tabBar().tabRect(tabs.indexOf(self.catalogs[role]))
        dialog = self.dialogs.get(role)
        if dialog is not None and widget is (dialog.panel.table if role == "receptor" else dialog.table):
            rect = widget.rect()
            rect.setHeight(min(rect.height(), 100))
            return rect
        return widget.rect()

    def _bind_import_catalog(self, role):
        for dialog in self.dialogs.values():
            dialog.hide()  # Back can leave a modal import dialog before it submits.
        view_id = RECEPTOR_VIEW_ID if role == "receptor" else LIGANDS_VIEW_ID
        catalog = self.window.central_widget.open_view(view_id)
        if catalog is None:
            raise ValueError(f"The {role} catalog is unavailable.")
        self.catalogs[role] = catalog
        button = catalog.findChild(QPushButton, "EmptyActionButton")
        if button is None:
            raise ValueError(f"The {role} catalog Import control is unavailable.")
        self.import_buttons[role] = button

    def _bind_import_dialog(self, role, dialog):
        if not self.controller.is_active or self.controller.current_step.target != role:
            return
        self.dialogs[role] = dialog
        if role == "receptor":
            dialog.panel.file_dialog_directory = str(self.inputs)
            dialog.panel.file_dialog_options = QFileDialog.Option.DontUseNativeDialog
            dialog.panel.ready_changed.connect(lambda *_: self._validate_import_choice(role))
        else:
            dialog.file_dialog_directory = str(self.inputs)
            dialog.file_dialog_options = QFileDialog.Option.DontUseNativeDialog
            dialog.table.rows_changed.connect(lambda: self._validate_import_choice(role))
        dialog.jobs_submitted.connect(lambda jobs: self._track(role, jobs))
        dialog.rejected.connect(self.controller.stop)
        self._validate_import_choice(role)
        # The normal import action shows the dialog. Refresh on its event loop so
        # the spotlight follows the user's click into the now-visible window.
        QTimer.singleShot(0, self.controller.refresh)

    def _bind_studio(self):
        if self.studio is None:
            self.studio = self.window.tools.widgets[DOCKING_VIEW_ID]
            self.studio.ligand_prep_jobs.finished.connect(lambda status: self._prepared("ligand", status))
            self.studio.receptor_prep_jobs.finished.connect(lambda status: self._prepared("receptor", status))
            grid = self.window.grid_dock
            if grid is not None:
                grid.binding_site_changed.connect(self._grid_changed)

    def _import_paths(self, role):
        dialog = self.dialogs[role]
        paths = dialog.panel.file_paths if role == "receptor" else [path for path, _ in dialog.table.rows()]
        return [str(Path(path).resolve()) for path in paths]

    def _correct_import(self, role):
        paths = self._import_paths(role)
        if len(paths) != 1:
            return False
        try:
            path = Path(paths[0])
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if key not in self._file_hashes:
                self._file_hashes[key] = hashlib.sha256(path.read_bytes()).digest()
            return self._file_hashes[key] == self._expected_hashes[role]
        except OSError:
            return False

    def _validate_import_choice(self, role):
        dialog = self.dialogs[role]
        correct = self._correct_import(role)
        ready = dialog.panel.is_ready() if role == "receptor" else bool(dialog.table.rows())
        dialog.import_button.setEnabled(correct and ready)
        self._feedback = ""

    def _receptor_target(self):
        if self.studio.stepper.current_index != 2:
            return self.studio.stepper
        if self._grid_ready:
            return self.studio.prepare_receptor_button
        grid = self.window.grid_dock
        if grid is not None and grid.isVisible() and grid._current_molecule is not None:
            if not grid._working.dirty and grid._working.source_site_id is None:
                return grid.new_button
            if not self._box_values_match(grid):
                return grid.box_settings
            if grid._working.dirty:
                return grid.save_button
            if grid._selected_site() is None:
                return grid.site_tree
            return grid.activate_button if grid.activate_button.isEnabled() else grid
        if grid is not None and grid._current_molecule is not None and self.window.pymol_dock is not None:
            return self.window.pymol_dock
        return self.window.central_widget

    def _box_values_match(self, grid):
        return all(abs(spin.value() - value) <= 0.02 for spin, value in zip(
            (grid.cx, grid.cy, grid.cz, grid.sx, grid.sy, grid.sz),
            (*self.example["grid_center"], *self.example["grid_size"])))

    def _run_target(self):
        if not self.studio.isVisible():
            return self.window.tools.action_buttons[DOCKING_VIEW_ID]
        return self.studio.run_button if self.studio.stepper.current_index == 3 else self.studio.stepper

    def _grid_changed(self, *_):
        if not self.controller.is_active:
            return
        if self._grid_checking:
            self._grid_recheck = True
            return
        self._grid_checking = True
        def read():
            sites = list(self.runtime.binding_sites.select())
            return any(site.is_active and all(abs(value - expected) <= 0.02 for value, expected in zip(
                (site.center_x, site.center_y, site.center_z, site.size_x, site.size_y, site.size_z),
                (*self.example["grid_center"], *self.example["grid_size"]))) for site in sites)
        def done(ready):
            self._grid_checking = False
            if self.controller.is_active:
                self._grid_ready = ready
                if self._grid_recheck:
                    self._grid_recheck = False
                    self._grid_changed()
        def error(exc):
            self._grid_checking = False
            self._note(str(exc))
        run_async(read, done, on_error=error)

    def _instruction(self, step):
        text = step.text
        role = {"load-receptor": "receptor", "load-ligand": "ligand"}.get(step.id)
        if step.id == "create-project":
            target = self._project_target()
            form = self._project_form
            if form is not None and form.isVisible():
                if target is form.name_input:
                    text = "Enter a name for your new practice project, for example Vps34 docking tutorial, then press Enter."
                else:
                    text = "Click Save to create and open your project."
            elif self.inputs is not None:
                text = "Your practice project is created."
            elif self._project_browser is not None and self._project_browser.isVisible():
                text = "Click New project. You will name it in the next window."
        elif role is not None:
            dialog = self.dialogs.get(role)
            filename = self.example[role]
            if role in self.pending:
                text = f"The {role} import is running. Wait for it to finish."
            elif dialog is not None and dialog.isVisible():
                paths = self._import_paths(role)
                if not paths:
                    text = (f"Click Add files. In the file chooser, select {filename} and click Open. "
                            "The chooser opens in the practice project's inputs folder.")
                elif not self._correct_import(role):
                    text = (f"These files do not match the bundled {filename}. Select the file rows "
                            "(Ctrl+A selects all), click Remove, then Add files to choose "
                            f"{self.inputs / filename}.")
                elif not dialog.import_button.isEnabled():
                    text = "The receptor preview is loading. Wait for it to finish before reviewing the import."
                else:
                    text = (f"Review {filename} in the file list. "
                            + ("Keep chain A and the default import options. " if role == "receptor" else "Keep Small molecule and the default import options. ")
                            + "Click Import to load it into the project.")
            elif not self.catalogs[role].isVisible() and not self.outcomes.get(role):
                text = f"Click the {role.title()}s tab above the table, then click its central Import button."
        elif step.id == "programs":
            text = ("Click Programs in Docking Studio if needed. Choose AutoDock Vina in the program list, "
                    "check Run AutoDock Vina and uncheck other programs. Keep Docking, Standard and Small molecule "
                    "with the default Vina settings. Click Next in the Programs panel to continue to Ligands.")
        elif step.id == "prepare-ligand":
            if self.studio.ligand_prep_jobs.job_ids:
                text = "Ligand preparation is running. Wait for it to complete before preparing the receptor."
            elif self.studio.stepper.current_index != 1:
                text = "Click Ligands in the Docking Studio header, then click Prepare Ligands. Wait for preparation to complete."
        elif step.id == "prepare-receptor" and not self._grid_ready:
            grid = self.window.grid_dock
            if self.studio.stepper.current_index != 2:
                text = "Click Receptors in the Docking Studio header to prepare the receptor and define its docking box."
            elif grid is None or grid._current_molecule is None:
                text = "Click the Vps34 receptor row in the Receptors table to show its structure and Grid Box controls."
            elif not grid.isVisible():
                text = "Click Grid Box on the PyMOL toolbar to open the box controls."
            elif not grid._working.dirty and grid._working.source_site_id is None:
                text = "Click New in the Grid Box panel to create a working docking box."
            elif not self._box_values_match(grid):
                # The spin boxes parse the user's locale: 18,93 where the decimal mark is a comma.
                number = lambda value: grid.cx.locale().toString(int(value))
                center = " / ".join(map(number, self.example["grid_center"]))
                size = " / ".join(map(number, self.example["grid_size"]))
                text = (f"Enter Center X / Y / Z = {center} Å and Size X / Y / Z = {size} Å. "
                        "These values cover the original SAR405 binding site.")
            elif grid._working.dirty:
                text = "The box values match the example. Click Save to store this docking site."
            elif grid._selected_site() is None:
                text = "Select the site you just saved in the Grid Box list."
            elif grid.activate_button.isEnabled():
                text = "Click Set Active so docking uses the saved box."
            else:
                text = "Checking the saved active box. Then click Prepare Receptors in Docking Studio."
        elif step.id == "run-docking":
            if not self.studio.isVisible():
                text = "Click Docking Studio to review the run and try again."
            elif self.studio.isVisible() and self.studio.stepper.current_index != 3:
                text = "Click Preview & Run in the Docking Studio header. Review the checks, then click Run Docking."
        return text + ("\n\n" + self._feedback if self._feedback else "")

    def _track(self, stage, jobs):
        if not self.controller.is_active:
            return
        self._feedback = ""
        self.pending[stage] = {str(job) for job in jobs if job}
        self.outcomes[stage] = {}
        self.completed_stages.discard(stage)
        if stage in self.dialogs:
            # Move the overlay before QDialog.accept() closes its previous host. Closing
            # the importer is successful submission, not cancellation of the tutorial.
            self.controller.refresh()
        self.window.monitor_bridge.request_refresh()

    def _job_finished(self, job_id, status):
        if not self.controller.is_active:
            return
        for stage, jobs in list(self.pending.items()):
            if str(job_id) not in jobs:
                continue
            self.outcomes[stage][str(job_id)] = STATUS.get(status, status)
            if not jobs <= self.outcomes[stage].keys():
                continue
            del self.pending[stage]
            if set(self.outcomes[stage].values()) != {"completed"}:
                self._note("This job did not complete successfully. Open Jobs for details, then retry the step.")
                if stage in self.dialogs:
                    self.dialogs[stage].show()
                continue
            self._check(stage)

    def _prepared(self, role, status):
        if self.controller.is_active and status == "completed":
            self._check(f"prepare_{role}")
        elif self.controller.is_active:
            self._note("Preparation did not complete successfully. Review the job details and retry preparation.")

    def _check(self, stage):
        if stage in self._checking:
            return
        self._checking.add(stage)
        generation = self._generation

        def read():
            role = stage.removeprefix("prepare_")
            scope = self.runtime.molecules.select(role=role)
            if stage.startswith("prepare_"):
                counts = self.runtime.docking.preparation_summary(scope.scope, role_type=role)
                return counts["total"] == counts["ready"] == 1 and (role != "receptor" or self._grid_ready)
            if scope.count() != 1:
                return False
            return True

        def done(ok):
            self._checking.discard(stage)
            if not self.controller.is_active or generation != self._generation:
                return
            if not ok:
                self._note("The expected molecule or result is not ready. Review the job details and retry this step.")
                return
            signal = {"receptor": self.receptor_loaded, "ligand": self.ligand_loaded,
                      "prepare_ligand": self.ligand_prepared, "prepare_receptor": self.receptor_prepared}[stage]
            self.completed_stages.add(stage)
            signal.emit()

        def error(exc):
            self._checking.discard(stage)
            if self.controller.is_active and generation == self._generation:
                self._note(f"Could not check this step: {exc}")

        run_async(read, done, on_error=error)

    def _note(self, message):
        self._feedback = message
        self.controller.refresh()

    def _cleanup(self, *_):
        self._generation += 1
        self.pending.clear()
        from shiboken6 import isValid
        if self._project_form is not None and isValid(self._project_form):
            self._project_form.name_input.removeEventFilter(self)
        for dialog in self.dialogs.values():
            if dialog.isVisible() or dialog.result() != QDialog.Accepted:
                dialog.reject()
        try:
            self.window.monitor_bridge.job_finished.disconnect(self._job_finished)
            self.window.import_dialog_opened.disconnect(self._bind_import_dialog)
            self.window._app_widget.projects_opened.disconnect(self._bind_projects)
            self.window.project_opened.disconnect(self._project_opened)
            if self._project_browser is not None:
                self._project_browser.project_created.disconnect(self._remember_created_project)
                self._project_browser.project_form_opened.disconnect(self._bind_project_form)
        except RuntimeError:
            pass

    def _finish(self, *_):
        self._cleanup()
        self.window.open_or_focus_view(COMPLEXES_VIEW_ID)
        self.window.dock_manager.toggle("tools", False)
        # Return to the submit handler before showing a dialog so it can start its worker.
        QTimer.singleShot(0, lambda: QMessageBox.information(self.window, "Tutorial completed",
            "Docking is being submitted. When the calculation finishes, open Docking Results "
            "to inspect its poses and scores. You can check Jobs for its progress or errors."))

    def _failed(self, _tutorial_id, message):
        self._cleanup()
        QMessageBox.warning(self.window, "Simple docking tutorial", message)
