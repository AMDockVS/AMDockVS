from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    # Kept out of module import time: pulling in the runtime (MolSuite, executors) here would
    # delay the splash by ~2s. It's imported inside main() only after the splash is on screen.
    from amdockvs.runtime import AMDockVSRuntime


def _splash_disabled() -> bool:
    return (
            os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen"
            or os.environ.get("AMDOCK_DISABLE_SPLASH", "").strip().lower() in {"1", "true", "yes", "on"}
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amdockvs")
    parser.add_argument("-pi", "--project-id", help="UUID of the project to open at start-up.")
    parser.add_argument("-pp", "--project-path", help="Path of the project to open at start-up.")
    return parser


FREEZE_LOG = "/tmp/amdock_stacks.log"


def install_freeze_watchdog(qt_app, *, stall_seconds: float = 10.0):
    """Dump every thread's Python stack to :data:`FREEZE_LOG` while the GUI thread is stuck.

    A frozen window can't be inspected from outside here (``ptrace_scope=1`` blocks py-spy
    and gdb), so the app reports itself: a 500 ms timer pats a heartbeat, and a plain
    daemon thread dumps the stacks whenever that heartbeat goes stale. ``kill -USR1 <pid>``
    forces a dump too. Returns the timer -- Qt drops timers that nobody holds.
    """
    import faulthandler
    import signal
    import threading
    import time

    from PySide6.QtCore import QTimer

    log = open(FREEZE_LOG, "a", buffering=1)  # noqa: SIM115 - lives as long as the app
    faulthandler.register(signal.SIGUSR1, file=log, all_threads=True)
    beat = [time.monotonic()]
    timer = QTimer(qt_app)
    timer.setInterval(500)
    timer.timeout.connect(lambda: beat.__setitem__(0, time.monotonic()))
    timer.start()

    def watch() -> None:
        while True:
            time.sleep(2.0)
            stalled = time.monotonic() - beat[0]
            if stalled < stall_seconds:
                continue
            print(f"\n=== GUI thread stalled {stalled:.0f}s at {time.strftime('%H:%M:%S')} ===", file=log)
            faulthandler.dump_traceback(file=log, all_threads=True)
            beat[0] = time.monotonic()  # one dump per stall window, not per poll

    threading.Thread(target=watch, name="freeze-watchdog", daemon=True).start()
    return timer


def _open_startup_project(runtime: AMDockVSRuntime, *, project_id: str | None, project_path: str | None) -> None:
    if project_id and project_path:
        raise ValueError("Use --project-id or --project-path, not both.")
    if project_id:
        runtime.open_project(project_id)
        return
    if not project_path:
        return
    path = Path(project_path).expanduser().resolve()
    project = runtime.molsuite.find_project(folder=path)
    if project is None:
        raise ValueError(f"No AMDockVS project is registered at: {path}")
    runtime.open_project(project.id)


def main(argv: Sequence[str] | None = None) -> int:
    import time
    start = time.time()
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Native file dialogs: our conda Qt ships the theme plugins but leaves QT_QPA_PLATFORMTHEME
    # unset, so Qt loads no platform theme and falls back to its own (non-native) dialog. Route
    # dialogs through the xdg-desktop-portal (D-Bus to the running GTK portal) — the gtk3 plugin
    # can't be linked here (conda libpangoft2 vs harfbuzz symbol clash), but the portal needs
    # none of that. Overridable, and skipped for offscreen tests. Must precede QApplication().
    if not os.environ.get("QT_QPA_PLATFORMTHEME") and \
            os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen":
        os.environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    qt_app = QApplication.instance()
    owns_app = qt_app is None
    if qt_app is None:
        # PyMOL is rendered by a QOpenGLWidget. Moving a dock between this main
        # window and a native top-level window otherwise destroys that widget's
        # OpenGL context while PyMOL still owns resources created in it. The next
        # pymol.draw() can then abort the whole process in native code. Qt must be
        # told to preserve/share GUI OpenGL contexts before QApplication exists.
        QCoreApplication.setAttribute(
            Qt.ApplicationAttribute.AA_ShareOpenGLContexts,
            True,
        )
        qt_app = QApplication(["amdockvs"])

    watchdog = install_freeze_watchdog(qt_app)  # noqa: F841 - the timer must outlive this scope

    from ms_components.theme import apply_theme
    from ms_components.wheel import install_shift_hscroll, uninstall_shift_hscroll

    from amdockvs.ui.theme import saved_base_font_pt, saved_theme_name
    from amdockvs.ui.resources.icons.themed import shutdown_themed_icons
    apply_theme(saved_theme_name(), qt_app,
                base_font_pt=saved_base_font_pt())  # ribbon recolor follows in main_window; live switch via Theme menu
    install_shift_hscroll(qt_app)  # Shift+wheel = horizontal, as in the browser/GTK

    print("Creating splash…", time.time() - start)
    splash = None
    if not _splash_disabled():
        from amdockvs.ui.splash import create_splash
        print("Creating splash 2…", time.time() - start)
        splash = create_splash()

    if splash is not None:
        splash.status("Starting engine…")
    print("Starting engine WITHOUT IMPORTS...", time.time() - start)

    from amdockvs.runtime import AMDockVSRuntime
    print("Starting engine...", time.time() - start)
    runtime = AMDockVSRuntime()
    startup_error: Exception | None = None
    try:
        if splash is not None:
            splash.status("Opening project…")
        try:
            _open_startup_project(
                runtime,
                project_id=args.project_id,
                project_path=args.project_path,
            )
        except Exception as exc:
            startup_error = exc

        if splash is not None:
            splash.status("Loading interface…")
        from amdockvs.ui.main_window import AMDockVSMainWindow

        window = AMDockVSMainWindow(runtime=runtime)
        window.showMaximized()
        # Bring the window to front and let it paint NOW, so it sits maximized behind the
        # (always-on-top) splash — PyCharm-style — instead of only appearing when the splash closes.
        window.raise_()
        window.activateWindow()
        print("Loading interface...", time.time() - start)
        if splash is not None:
            qt_app.processEvents()
            splash.raise_()
            splash.finish(window)
            print('splash finished', time.time() - start)
        if startup_error is not None:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(window, "Open Project", f"Could not open the startup project:\n{startup_error}")
        return qt_app.exec()
    finally:
        try:
            uninstall_shift_hscroll(qt_app)
            shutdown_themed_icons(qt_app)
        finally:
            runtime.shutdown()
            if owns_app:
                qt_app.quit()
