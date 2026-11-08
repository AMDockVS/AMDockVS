"""Run the eight-step UI path with real workers and Vina; write screenshots for review.

Usage: python test/e2e/simple_docking_walkthrough.py /tmp/tutorial-review
AMDockVS must be importable, installed or through PYTHONPATH=src.
"""
import os
import json
from pathlib import Path
import sys
import time


def main(root, *, cancel_during_preview=False, native=False, slow_submission=False, prepare_before_box=False,
         strict_input=False, program_navigation=False):
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(AMDOCK_HOME=str(root / "amdock"), MOLSUITE_HOME=str(root / "molsuite"),
                      AMDOCK_CCD_CACHE=str(root / "ccd"))
    if native:
        os.environ.pop("AMDOCK_DISABLE_PYMOL", None)
    else:
        os.environ.update(QT_QPA_PLATFORM="offscreen", AMDOCK_DISABLE_PYMOL="1")
    from amdockvs.runtime import AMDockVSRuntime
    from PySide6.QtCore import QPoint, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDockWidget,
                                  QFileDialog, QLineEdit, QMessageBox, QPushButton)
    from ms_components.theme import apply_theme
    from amdockvs.ui.main_window import AMDockVSMainWindow
    from amdockvs.ui.view_ids import DOCKING_VIEW_ID

    if native:
        from ms_components.opengl import prepare_pymol_opengl

        prepare_pymol_opengl()
    app = QApplication(["tutorial-walkthrough"])
    if native:
        assert app.platformName() not in {"offscreen", "minimal"}
    apply_theme("dark", app)
    runtime = AMDockVSRuntime()
    window = AMDockVSMainWindow(runtime=runtime)
    if native:
        grid = window.grid_dock
        assert grid is not None and window.pymol_dock is not None
    else:
        # Reuse the real box editor without starting an OpenGL/PyMOL context.
        from amdockvs.ui.tools.binding_sites.box import GridBoxSettingDockWidget
        grid_host = QDockWidget("Grid Box", window)
        grid = GridBoxSettingDockWidget(runtime=runtime, parent=grid_host)
        window.grid_dock = grid
        grid_host.setWidget(grid)
        window.addDockWidget(Qt.RightDockWidgetArea, grid_host)
        grid_host.hide()
    window.show()
    for width, height in ((900, 700), (1400, 900)):
        window.resize(width, height)
        app.processEvents()
        window.grab().save(str(root / f"welcome-{width}.png"))
    if native:
        window.resize(*map(int, os.environ.get("AMDOCK_TEST_VIEWPORT", "1920x936").split("x")))
        window.move(app.primaryScreen().availableGeometry().topLeft())
        app.processEvents()
    # Give the form a temporary default without browsing or touching the user's projects.
    from ms_components.ms_projects.browser import ProjectFormDialog
    original_form_init = ProjectFormDialog.__init__
    def isolated_form_init(self, *args, **kwargs):
        if kwargs.get("mode") == "create":
            kwargs["initial_data"] = {**kwargs.get("initial_data", {}), "path": str(root)}
        original_form_init(self, *args, **kwargs)
    ProjectFormDialog.__init__ = isolated_form_init
    # Observe the actual chooser created by getOpenFileNames, with no file
    # selected until the test types and clicks Open.
    pickers = []
    original_choose_files = QFileDialog.getOpenFileNames
    def choose_files(parent, title, directory, name_filter, **kwargs):
        assert kwargs["options"] & QFileDialog.Option.DontUseNativeDialog
        role = "receptor" if "receptor" in title.lower() else "ligand"
        def observe_chooser():
            chooser = QApplication.activeModalWidget()
            assert isinstance(chooser, QFileDialog)
            assert Path(chooser.directory().absolutePath()) == Path(directory)
            assert chooser.findChild(QLineEdit, "fileNameEdit").text() == ""
            pickers.append((role, chooser))
        QTimer.singleShot(0, observe_chooser)
        return original_choose_files(parent, title, directory, name_filter, **kwargs)
    QFileDialog.getOpenFileNames = choose_files
    messages, errors = [], []
    QMessageBox.information = lambda *args: messages.append(args[1])
    QMessageBox.warning = lambda *args: errors.append(str(args[1:3]))
    QMessageBox.critical = lambda *args: errors.append(str(args[1:3]))
    window.tutorials_panel.start_button.click()
    tutorial = window._docking_tutorial
    if strict_input:
        mouse_click = QTest.mouseClick

        def visible_mouse_click(widget, button, *args, **kwargs):
            pos = kwargs.get("pos", widget.rect().center())
            point = widget.mapToGlobal(pos)
            hit = QApplication.widgetAt(point)
            if hit is not widget and (hit is None or not widget.isAncestorOf(hit)):
                message = (f"Mouse click on {type(widget).__name__} ({widget.objectName()}) is blocked by "
                           f"{type(hit).__name__} ({hit.objectName() if hit is not None else ''}); "
                           f"tutorial target={tutorial.controller._target}; "
                           f"instruction={tutorial.controller.overlay.body.toPlainText()}")
                errors.append(message)
                raise AssertionError(message)
            mouse_click(hit, button, *args, **{**kwargs, "pos": hit.mapFromGlobal(point)})

        QTest.mouseClick = visible_mouse_click
    started = time.monotonic()
    seen, clicked = set(), set()
    result = [1]
    graphics = {"platform": app.platformName(), "checks": []}
    inspection_started = None

    def check_pymol(label):
        from ms_components.opengl import pymol_context_error

        viewport = window.pymol_dock.pymol_widget
        context = viewport.context()
        assert pymol_context_error(context) is None
        assert window.pymol_dock.cmd.count_atoms() > 0
        frame = viewport.grabFramebuffer()
        assert not frame.isNull() and frame.width() >= 100 and frame.height() >= 100
        colors = {frame.pixelColor(x, y).rgba()
                  for x in range(0, frame.width(), max(1, frame.width() // 40))
                  for y in range(0, frame.height(), max(1, frame.height() // 40))}
        assert len(colors) > 8, "The PyMOL framebuffer contains no rendered structure"
        frame.save(str(root / f"pymol-{label}.png"))
        graphics["checks"].append({"stage": label, "renderer": window.pymol_dock.cmd.get_renderer(),
                                   "version": context.format().version(), "profile": context.format().profile().name,
                                   "framebuffer": [frame.width(), frame.height()], "colors": len(colors)})
        (root / "graphics.json").write_text(json.dumps(graphics, indent=2))

    def click(button, index, pos=None):
        if button.isVisible() and button.isEnabled():
            clicked.add(index)
            # Catalog Import enters QDialog.exec(). Schedule outside this timer
            # callback so subsequent ticks can operate that modal dialog.
            def perform_click():
                if index in ("box-new", "box-saved", "box-active"):
                    window.grab().save(str(root / f"guidance-{index}.png"))
                if index == 6:
                    run_clicked_at = time.monotonic()
                    def check_after_run():
                        try:
                            elapsed = time.monotonic() - run_clicked_at
                            assert elapsed < 4.5, f"UI did not respond for {elapsed:.1f} seconds"
                            assert not window.tools_dock.isVisible()
                            assert window.central_widget.mapTo(window, QPoint()).x() < 150
                            window.grab().save(str(root / "after-run-2.5s.png"))
                            seen.add("after-run-2.5s")
                        except Exception as exc:
                            errors.append(str(exc))
                            app.quit()
                    QTimer.singleShot(2500, check_after_run)
                QTest.mouseClick(button, Qt.LeftButton, pos=pos or button.rect().center())
            QTimer.singleShot(0, perform_click)

    def tick():
        nonlocal inspection_started
        try:
            if prepare_before_box and isinstance(app.activeModalWidget(), QMessageBox):
                box = app.activeModalWidget()
                assert "no defined, active search box" in box.text()
                next(button for button in box.buttons() if button.text() == "Continue and skip").click()
                return
            if time.monotonic() - started > 240:
                raise RuntimeError(f"Tutorial timed out: step {tutorial.controller.index}, {errors}")
            for role, chooser in pickers:
                from shiboken6 import isValid
                if isValid(chooser) and chooser.isVisible() and f"choose-{role}" not in clicked:
                    clicked.add(f"choose-{role}")
                    chooser.grab().save(str(root / f"file-picker-{role}.png"))
                    def select(chooser=chooser, role=role):
                        edit = chooser.findChild(QLineEdit, "fileNameEdit")
                        edit.setFocus()
                        QTest.keyClicks(edit, tutorial.example[role])
                        button = chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.Open)
                        QTest.mouseClick(button, Qt.LeftButton)
                    QTimer.singleShot(0, select)
                    return
            if tutorial.controller.is_active and tutorial.controller.current_step.id == "create-project":
                target = tutorial._project_target()
                form = tutorial._project_form
                if "project-stage" not in seen:
                    seen.add("project-stage")
                    window.grab().save(str(root / "step-1.png"))
                    assert runtime.active_context is None
                    assert runtime.list_projects(page=1, items_per_page=100) == []
                if form is not None and form.isVisible():
                    if not form.name_input.text():
                        form.grab().save(str(root / "project-empty.png"))
                        QTest.keyClicks(form.name_input, "My ")
                        QTest.qWait(120)
                        assert tutorial._project_target() is form.name_input
                        QTest.keyClicks(form.name_input, "Vps34 tutorial")
                        QTest.keyClick(form.name_input, Qt.Key_Return)
                    elif target is form.save_button:
                        form.grab().save(str(root / "project-ready.png"))
                        if "project-save" not in clicked:
                            assert QApplication.focusWidget() is form.save_button
                            clicked.add("project-save")
                            QTimer.singleShot(0, lambda: QTest.keyClick(form.save_button, Qt.Key_Return))
                elif "project-open" not in clicked:
                    click(target, "project-open")
                elif "project-new" not in clicked:
                    click(target, "project-new")
                return
            if cancel_during_preview:
                if "preview-cancelled" in seen:
                    if not tutorial.dialogs["receptor"].panel.preview_running:
                        result[0] = 0
                        app.quit()
                    return
                if "receptor" not in tutorial.dialogs:
                    if "open-receptor" not in clicked:
                        click(tutorial.import_buttons["receptor"], "open-receptor")
                    return
                if not tutorial._import_paths("receptor"):
                    if "add-receptor" not in clicked:
                        click(tutorial.controller.targets["receptor"].widget(), "add-receptor")
                    return
                if tutorial.dialogs["receptor"].panel.preview_running:
                    seen.add("preview-cancelled")
                    tutorial.controller.stop()
                    assert not tutorial.controller.is_active
                elif time.monotonic() - started > 5:
                    raise RuntimeError("The preview scanner was not observed running")
                return
            if not tutorial.controller.is_active:
                stats = runtime.docking.result_stats()
                if errors:
                    print(errors, file=sys.stderr, flush=True)
                    raise RuntimeError("Docking submission failed")
                if stats.completed_results != 1:
                    if messages == ["Tutorial completed"]:
                        from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID

                        assert not window.tools_dock.isVisible()
                        assert window.central_widget.current_view_id() == COMPLEXES_VIEW_ID
                        assert window.central_widget.mapTo(window, QPoint()).x() < 150, "Hidden Docking Studio left an empty column"
                        if "finished-before-result" not in seen:
                            window.grab().save(str(root / "submitting.png"))
                        seen.add("finished-before-result")
                    return
                if "after-run-2.5s" not in seen:
                    return
                if slow_submission:
                    assert "finished-before-result" in seen, "Tutorial did not finish before the calculation"
                assert not window.tools_dock.isVisible(), "Docking Studio should close after submission"
                from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID

                results_view = window.central_widget.open_view(COMPLEXES_VIEW_ID)
                results = results_view.stack.currentWidget()
                if "refresh-completed-results" not in seen:
                    seen.add("refresh-completed-results")
                    results_view.refresh()
                    return
                state = {"receptor": results._selected_receptor_id, "ligand": results._selected_ligand_id,
                         "program": results.protocol_combo.currentData(),
                         "counts": {name: getattr(results, f"{name}_table").record_total
                                    for name in ("receptor", "ligand", "pose")}}
                if graphics.get("result_state") != state:
                    graphics["result_state"] = state
                    (root / "graphics.json").write_text(json.dumps(graphics, indent=2))
                    print("Result inspection:", state, flush=True)
                if inspection_started is None:
                    inspection_started = time.monotonic()
                if time.monotonic() - inspection_started > 30:
                    raise RuntimeError(f"Result inspection stalled: {state}")
                for name in ("receptor", "ligand", "pose"):
                    table = getattr(results, f"{name}_table").view
                    item = table.model().index(0, 0)
                    if not item.isValid() or item.data() is None:
                        return
                    if f"inspect-{name}" not in seen:
                        seen.add(f"inspect-{name}")
                        QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(item).center())
                        return
                if results.d_score.text() == "-":
                    return
                if native:
                    pose_object = window.pymol_dock.scene_context.selections.get("ligand", "")
                    if not pose_object.startswith("amdock_result_pose"):
                        return
                    if window.pymol_dock.cmd.count_atoms(pose_object) == 0:
                        return
                    graphics["score"] = float(results.d_score.text())
                    graphics["pose_atoms"] = window.pymol_dock.cmd.count_atoms(pose_object)
                    check_pymol("completed")
                window.grab().save(str(root / "completed.png"))
                result[0] = 0 if messages == ["Tutorial completed"] and stats.completed_results == 1 else 1
                print(stats.model_dump_json(), flush=True)
                app.quit()
                return
            if errors or time.monotonic() - started > 240:
                raise RuntimeError(f"Tutorial did not finish: step {tutorial.controller.index}, {errors}")
            index = tutorial.controller.index - 1
            if index not in seen:
                seen.add(index)
                tutorial.controller.overlay.parentWidget().grab().save(str(root / f"step-{index + 2}.png"))
                print(f"Step {index + 2}: {tutorial.controller.current_step.id}", flush=True)
            if index in clicked:
                return
            if index in (0, 1):
                role = "receptor" if index == 0 else "ligand"
                if role not in tutorial.dialogs:
                    if not tutorial.catalogs[role].isVisible():
                        if f"catalog-{role}" not in clicked:
                            binding = tutorial.controller.targets[role]
                            click(binding.widget(), f"catalog-{role}", binding.rect().center())
                    elif f"open-{role}" not in clicked:
                        click(tutorial.import_buttons[role], f"open-{role}")
                else:
                    if f"dialog-{role}" not in seen:
                        seen.add(f"dialog-{role}")
                        assert tutorial._import_paths(role) == []
                        tutorial.dialogs[role].grab().save(str(root / f"import-{role}-empty.png"))
                    if not tutorial._import_paths(role):
                        if f"add-{role}" not in clicked:
                            click(tutorial.controller.targets[role].widget(), f"add-{role}")
                    else:
                        tutorial.dialogs[role].grab().save(str(root / f"import-{role}.png"))
                        click(tutorial.dialogs[role].import_button, index)
            elif index == 2:
                click(window.tools.action_buttons[DOCKING_VIEW_ID], index)
            elif index == 3:
                if program_navigation and "program-navigation" not in seen:
                    seen.add("program-navigation")
                    # Model the installed QuickVina default without requiring that
                    # executable: this run switches to Vina before doing any work.
                    tutorial.studio._check_programs = lambda: None
                    tutorial.studio._apply_missing_programs({
                        key: reason for key, reason in tutorial.studio._missing_programs.items()
                        if key != "qvina"
                    })
                    for key, check in tutorial.studio._program_checks.items():
                        check.setChecked(key == "qvina")
                    tutorial.studio.stepper.go_to(tutorial.studio.step_ligands)
                    return
                if program_navigation and "prepare_ligand" not in tutorial.completed_stages:
                    # Reproduce the screenshot: real ligand preparation finishes
                    # and opens Receptors while the guide still waits at Programs.
                    # Set up this pre-existing state directly: the corrected guide
                    # now restricts mouse input to the header until Programs is fixed.
                    if "prepare-before-program-recovery" not in clicked:
                        clicked.add("prepare-before-program-recovery")
                        tutorial.studio._prepare_ligands()
                    return
                if tutorial.studio.stepper.current_index != 0:
                    assert "Return to Programs" in tutorial._instruction(tutorial.controller.current_step)
                    window.grab().save(str(root / "program-navigation-recovery.png"))
                    QTest.mouseClick(tutorial.studio.stepper._header_items[0], Qt.LeftButton)
                    return
                from amdockvs.docking.engines.programs import list_docking_programs
                keys = [spec.key for spec in list_docking_programs()]
                wrong = next((key for key, check in tutorial.studio._program_checks.items()
                              if key != "vina" and check.isChecked()), None)
                key = wrong or "vina"
                row = keys.index(key)
                if tutorial.studio.program_list.currentRow() != row:
                    item = tutorial.studio.program_list.item(row)
                    QTest.mouseClick(tutorial.studio.program_list.viewport(), Qt.LeftButton,
                                     pos=tutorial.studio.program_list.visualItemRect(item).center())
                    return
                check = tutorial.studio._program_checks[key]
                if check.isChecked() != (key == "vina"):
                    # The form can be wider than its viewport after showing PyMOL.
                    # Click the visible indicator rather than its clipped center.
                    QTest.mouseClick(check, Qt.LeftButton, pos=QPoint(8, check.height() // 2))
                    return
                tutorial.studio.exhaustiveness.setValue(1)
                button = next(button for button in tutorial.studio.findChildren(QPushButton)
                              if button.text() == "Next" and button.isVisible())
                click(button, index)
            elif index == 4:
                click(tutorial.studio.prepare_ligands_button, index)
            elif index == 5:
                if not tutorial._grid_ready:
                    assert not tutorial.studio.prepare_receptor_button.isEnabled()
                if prepare_before_box and "prepare-before-box" not in clicked:
                    # Simulate preparation started outside the tutorial. Its own button
                    # must stay disabled until the box has been defined and activated.
                    clicked.add("prepare-before-box")
                    tutorial.studio._prepare_receptors()
                    return
                if prepare_before_box and "prepared-before-box" not in seen:
                    if not tutorial._receptor_preparation_ready:
                        return
                    assert not tutorial._grid_ready
                    assert tutorial.studio.stepper.current_index == 3
                    seen.add("prepared-before-box")
                    window.grab().save(str(root / "prepared-before-box.png"))
                    # Revisit Receptors after preparation; its row must still be available
                    # for box definition, without forcing another preparation job.
                    tutorial.studio.stepper.set_current_index(2)
                    grid.clear_molecule()
                    return
                if tutorial.studio.stepper.current_index != 2:
                    QTest.mouseClick(tutorial.studio.stepper._header_items[2], Qt.LeftButton)
                    return
                if grid._current_molecule is None:
                    from amdockvs.ui.view_ids import RECEPTOR_VIEW_ID
                    table = window.central_widget.open_view(RECEPTOR_VIEW_ID).table.view
                    item = table.model().index(0, 0)
                    if item.isValid():
                        QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(item).center())
                    return
                if native and not strict_input and "pymol-detached" not in seen:
                    viewport = window.pymol_dock.pymol_widget
                    if viewport.context() is None or window.pymol_dock.cmd.count_atoms() == 0:
                        return
                    check_pymol("docked")
                    seen.add("pymol-detached")
                    window.pymol_dock.open_in_window()
                    return
                if native and not strict_input and "pymol-redocked" not in seen:
                    assert window.dock_manager.is_windowed("pymol")
                    tutorial.controller.refresh()
                    assert tutorial.controller.overlay.parentWidget() is window.pymol_dock.window()
                    window.pymol_dock.grab().save(str(root / "pymol-native-window.png"))
                    check_pymol("detached")
                    seen.add("pymol-redocked")
                    window.pymol_dock.dock_to_main_window()
                    return
                if native and not strict_input and "pymol-reattached" not in seen:
                    assert not window.dock_manager.is_windowed("pymol")
                    check_pymol("reattached")
                    seen.add("pymol-reattached")
                if not grid.isVisible():
                    if native:
                        action = window._pymol_grid_action
                        button = window.pymol_dock.control_bar.toolbar.widgetForAction(action)
                        click(button, "show-grid")
                    else:
                        grid_host.show()
                        grid.show()
                    return
                if "box-new" not in clicked:
                    grid.grab().save(str(root / "box-empty.png"))
                    click(grid.new_button, "box-new")
                elif "box-values" not in clicked:
                    if strict_input:
                        for spin, value in zip((grid.cx, grid.cy, grid.cz, grid.sx, grid.sy, grid.sz),
                                               (*tutorial.example["grid_center"], *tutorial.example["grid_size"])):
                            if abs(spin.value() - value) > 0.02:
                                QTest.mouseClick(spin, Qt.LeftButton)
                                QTest.keyClick(spin, Qt.Key_A, Qt.ControlModifier)
                                QTest.keyClicks(spin, spin.locale().toString(int(value)))
                                QTest.keyClick(spin, Qt.Key_Return)
                                return
                    clicked.add("box-values")
                    if not strict_input:
                        for spin, value in zip((grid.cx, grid.cy, grid.cz, grid.sx, grid.sy, grid.sz),
                                               (*tutorial.example["grid_center"], *tutorial.example["grid_size"])):
                            spin.setFocus()
                            QTest.keyClick(spin, Qt.Key_A, Qt.ControlModifier)
                            QTest.keyClicks(spin, spin.locale().toString(int(value)))
                            QTest.keyClick(spin, Qt.Key_Tab)
                    grid.grab().save(str(root / "box-values.png"))
                elif "box-saved" not in clicked:
                    click(grid.save_button, "box-saved")
                elif not tutorial._grid_ready:
                    if "box-active" not in clicked:
                        click(grid.activate_button, "box-active")
            elif index == 6:
                assert tutorial.controller.current_step.id == "run-docking"
                assert "Prepare receptor and run docking" in tutorial.controller.current_step.title
                if not tutorial._receptor_preparation_ready:
                    if "prepare-receptor" not in clicked:
                        window.grab().save(str(root / "final-card-before-preparation.png"))
                        click(tutorial.studio.prepare_receptor_button, "prepare-receptor")
                    else:
                        window.grab().save(str(root / "final-card-preparing.png"))
                    return
                if tutorial.studio.stepper.current_index != 3:
                    QTest.mouseClick(tutorial.studio.stepper._header_items[3], Qt.LeftButton)
                    return
                if "Ready to run" not in tutorial.studio.check_status_label.text():
                    return
                window.grab().save(str(root / "final-card-ready.png"))
                if slow_submission and "slow-submission" not in seen:
                    seen.add("slow-submission")
                    submit = tutorial.studio._submit_docking
                    def delayed_submit(*args, **kwargs):
                        time.sleep(5)
                        return submit(*args, **kwargs)
                    tutorial.studio._submit_docking = delayed_submit
                click(tutorial.studio.run_button, index)
        except Exception:
            import traceback

            result[0] = 1
            traceback.print_exc()
            window.grab().save(str(root / "failed.png"))
            tutorial.controller.stop()
            app.quit()

    timer = QTimer()
    timer.setInterval(50 if cancel_during_preview else 250)
    timer.timeout.connect(tick)
    timer.start()
    try:
        app.exec()
    finally:
        timer.stop()
        from shiboken6 import isValid
        if isValid(window):
            window.close()
        runtime.shutdown()
        app.processEvents()
    return result[0]


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve(), cancel_during_preview="--cancel-during-preview" in sys.argv[2:],
                         native="--native" in sys.argv[2:], slow_submission="--slow-submission" in sys.argv[2:],
                         prepare_before_box="--prepare-before-box" in sys.argv[2:],
                         strict_input="--strict-input" in sys.argv[2:],
                         program_navigation="--program-navigation" in sys.argv[2:]))
