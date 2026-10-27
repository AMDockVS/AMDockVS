"""Run the eight-step UI path with real workers and Vina; write screenshots for review.

Usage: python test/e2e/simple_docking_walkthrough.py /tmp/tutorial-review
AMDockVS must be importable, installed or through PYTHONPATH=src.
"""
import os
from pathlib import Path
import sys
import time


def main(root, *, cancel_during_preview=False):
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(AMDOCK_HOME=str(root / "amdock"), MOLSUITE_HOME=str(root / "molsuite"),
                      QT_QPA_PLATFORM="offscreen", AMDOCK_DISABLE_PYMOL="1",
                      AMDOCK_CCD_CACHE=str(root / "ccd"))
    from amdockvs.runtime import AMDockVSRuntime
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDockWidget,
                                  QFileDialog, QLineEdit, QMessageBox, QPushButton)
    from ms_components.theme import apply_theme
    from amdockvs.ui.main_window import AMDockVSMainWindow
    from amdockvs.ui.view_ids import DOCKING_VIEW_ID

    app = QApplication(["tutorial-walkthrough"])
    apply_theme("dark", app)
    runtime = AMDockVSRuntime()
    window = AMDockVSMainWindow(runtime=runtime)
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
    # The test chooses a temporary destination through the normal Browse action.
    # File selection below uses a real Qt
    # chooser, with no file selected until the test types and clicks Open.
    QFileDialog.getExistingDirectory = lambda *_args: str(root)
    pickers = []
    def choose_files(parent, title, directory, name_filter):
        chooser = QFileDialog(parent, title, directory, name_filter)
        chooser.setOption(QFileDialog.DontUseNativeDialog)
        chooser.setFileMode(QFileDialog.ExistingFiles)
        role = "receptor" if "receptor" in title.lower() else "ligand"
        assert chooser.findChild(QLineEdit, "fileNameEdit").text() == ""
        pickers.append((role, chooser))
        return (chooser.selectedFiles(), name_filter) if chooser.exec() == QDialog.Accepted else ([], "")
    QFileDialog.getOpenFileNames = choose_files
    messages, errors = [], []
    QMessageBox.information = lambda *args: messages.append(args[1])
    QMessageBox.warning = lambda *args: errors.append(str(args[1:3]))
    QMessageBox.critical = lambda *args: errors.append(str(args[1:3]))
    window.central_widget.docking_tutorial_button.click()
    tutorial = window._docking_tutorial
    started = time.monotonic()
    seen, clicked = set(), set()
    result = [1]

    def click(button, index, pos=None):
        if button.isVisible() and button.isEnabled():
            clicked.add(index)
            # Catalog Import enters QDialog.exec(). Schedule outside this timer
            # callback so subsequent ticks can operate that modal dialog.
            QTimer.singleShot(0, lambda: QTest.mouseClick(button, Qt.LeftButton, pos=pos or button.rect().center()))

    def tick():
        try:
            for role, chooser in pickers:
                if chooser.isVisible() and f"choose-{role}" not in clicked:
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
                        QTest.keyClick(form.name_input, Qt.Key_Tab)
                    elif not tutorial._project_path_chosen:
                        browse = next(button for button in target.findChildren(QPushButton) if button.text().startswith("Browse"))
                        click(browse, "project-browse")
                    else:
                        form.grab().save(str(root / "project-ready.png"))
                        click(target, "project-save")
                elif "project-open" not in clicked:
                    click(target, "project-open")
                elif "project-new" not in clicked:
                    click(target, "project-new")
                return
            if cancel_during_preview:
                if "receptor" not in tutorial.dialogs:
                    if "open-receptor" not in clicked:
                        click(tutorial.import_buttons["receptor"], "open-receptor")
                    return
                if not tutorial._import_paths("receptor"):
                    if "add-receptor" not in clicked:
                        click(tutorial.controller.targets["receptor"].widget(), "add-receptor")
                    return
                thread = tutorial.dialogs["receptor"].panel._scan_thread
                if thread is not None and thread.isRunning():
                    tutorial.controller.stop()
                    assert not thread.isRunning()
                    result[0] = 0
                    app.quit()
                elif time.monotonic() - started > 5:
                    raise RuntimeError("The preview scanner was not observed running")
                return
            if not tutorial.controller.is_active:
                stats = runtime.docking.result_stats()
                if errors:
                    print(errors, file=sys.stderr, flush=True)
                result[0] = 0 if messages == ["Tutorial completed"] and stats.completed_results == 1 else 1
                window.grab().save(str(root / "completed.png"))
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
                    QTest.mouseClick(check, Qt.LeftButton)
                    return
                button = next(button for button in tutorial.studio.findChildren(QPushButton)
                              if button.text() == "Next" and button.isVisible())
                click(button, index)
            elif index == 4:
                click(tutorial.studio.prepare_ligands_button, index)
            elif index == 5:
                if grid._current_molecule is None:
                    from amdockvs.ui.view_ids import RECEPTOR_VIEW_ID
                    table = window.central_widget.open_view(RECEPTOR_VIEW_ID).table.view
                    item = table.model().index(0, 0)
                    if item.isValid():
                        QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(item).center())
                    return
                if not grid.isVisible():
                    grid_host.show()
                    grid.show()
                    return
                if "box-new" not in clicked:
                    grid.grab().save(str(root / "box-empty.png"))
                    click(grid.new_button, "box-new")
                elif "box-values" not in clicked:
                    clicked.add("box-values")
                    for spin, value in zip((grid.cx, grid.cy, grid.cz, grid.sx, grid.sy, grid.sz),
                                           (*tutorial.example["grid_center"], *tutorial.example["grid_size"])):
                        spin.setFocus()
                        QTest.keyClick(spin, Qt.Key_A, Qt.ControlModifier)
                        QTest.keyClicks(spin, f"{value:.2f}")
                        QTest.keyClick(spin, Qt.Key_Tab)
                    grid.grab().save(str(root / "box-values.png"))
                elif "box-saved" not in clicked:
                    click(grid.save_button, "box-saved")
                elif not tutorial._grid_ready:
                    if "box-active" not in clicked:
                        click(grid.activate_button, "box-active")
                else:
                    click(tutorial.studio.prepare_receptor_button, index)
            elif index == 6 and "Ready to run" in tutorial.studio.check_status_label.text():
                click(tutorial.studio.run_button, index)
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
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
    raise SystemExit(main(Path(sys.argv[1]).resolve(), cancel_during_preview="--cancel-during-preview" in sys.argv[2:]))
