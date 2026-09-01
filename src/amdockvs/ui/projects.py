from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget

from ms_flow.api import ProjectCatalogBackend
from ms_components.ms_projects import ProjectsMenuWidget
from ms_components.ms_settings import AppSettingsDialog

if TYPE_CHECKING:
    from amdockvs.runtime import AMDockVSRuntime


class ApplicationWidget(QWidget):
    """Owns the Projects browser and Settings dialog, driven by the File menu.

    Not shown itself — the old ribbon backstage popup is gone; this is just a hidden
    controller that opens the dialogs and relays their signals to the main window.
    """

    project_requested = Signal(str)
    projects_closed = Signal()
    exit_requested = Signal()
    settings_saved = Signal()

    def __init__(self, runtime: AMDockVSRuntime, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        self._projects_widget: ProjectsWidget | None = None
        self._settings_dialog: AppSettingsDialog | None = None
        self.setObjectName("ApplicationWidget")
        self.hide()

    def open_projects(self) -> None:
        if self._projects_widget is None:
            self._projects_widget = ProjectsWidget(runtime=self.runtime, parent=self.parentWidget())
            self._projects_widget.project_requested.connect(self.project_requested.emit)
            self._projects_widget.closed.connect(self._on_projects_widget_closed)
            self._projects_widget.destroyed.connect(self._on_projects_widget_destroyed)
        self._projects_widget.refresh()
        self._projects_widget.show()
        self._projects_widget.raise_()
        self._projects_widget.activateWindow()

    def launch_project(self, project_id: str):
        if self._projects_widget is None:
            self._projects_widget = ProjectsWidget(runtime=self.runtime, parent=self.parentWidget())
            self._projects_widget.project_requested.connect(self.project_requested.emit)
            self._projects_widget.closed.connect(self._on_projects_widget_closed)
            self._projects_widget.destroyed.connect(self._on_projects_widget_destroyed)
        return self._projects_widget.launch_project(project_id)

    def open_settings(self) -> None:
        if self._settings_dialog is None:
            from amdockvs.ui.resources.icons import icon
            from amdockvs.ui.settings_tools import ExternalToolsPage

            self._settings_dialog = AppSettingsDialog(
                runtime=self.runtime,
                app_name="AMDockVS",
                icon_provider=icon,
                parent=self.parentWidget(),
            )
            self._settings_dialog.panel.add_page(
                ExternalToolsPage(self.runtime, parent=self._settings_dialog),
                "External tools",
            )
            self._settings_dialog.settings_saved.connect(self.settings_saved.emit)
            self._settings_dialog.destroyed.connect(self._on_settings_dialog_destroyed)
        else:
            self._settings_dialog.panel.reload_values()
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def get_total_projects(self) -> int:
        return len(self.runtime.list_projects(page=1, items_per_page=1000))

    def _on_projects_widget_closed(self) -> None:
        self._projects_widget = None
        self.projects_closed.emit()

    def _on_projects_widget_destroyed(self, *_args) -> None:
        self._projects_widget = None

    def _on_settings_dialog_destroyed(self, *_args) -> None:
        self._settings_dialog = None

    def closeEvent(self, event):
        if self._projects_widget is not None:
            self._projects_widget.close()
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        super().closeEvent(event)


class ProjectsWidget(QDialog):
    project_requested = Signal(str)
    closed = Signal()

    def __init__(self, *, runtime: "AMDockVSRuntime", parent: QWidget | None = None):
        super().__init__(parent=parent)

        self._owned_backend = ProjectCatalogBackend(
            app_id_filter=runtime.app_id,
            discover_apps=False,
            app_modules=["amdockvs.manifest"],
        )
        self.project_browser = ProjectsMenuWidget(
            app_id=runtime.app_id,
            backend=self._owned_backend,
            title="AMDock Projects",
            hint_text="Open, create and manage AMDock projects.",
            allow_create=True,
            open_after_create=True,
            allow_app_selection=False,
        )
        self.project_browser.project_requested.connect(self._on_project_requested)

        self.runtime = runtime
        self.setObjectName("ProjectsWidget")
        self.setWindowTitle("AMDock Projects")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(980, 620)

        layout = QVBoxLayout(self)
        # layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.project_browser)

    def showEvent(self, event):
        super().showEvent(event)
        # Center on the screen, not the parent: at startup the main window is still mid-maximize
        # (async on X11) so its geometry isn't final yet, which placed this dialog off-center.
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or self.screen() or QApplication.primaryScreen()
        if screen is not None:
            geo = self.frameGeometry()
            geo.moveCenter(screen.availableGeometry().center())
            self.move(geo.topLeft())

    def refresh(self) -> None:
        self.project_browser.refresh()

    def _on_project_requested(self, project_id: str) -> None:
        self.hide()
        self.project_requested.emit(project_id)

    def launch_project(self, project_id: str):
        return self._owned_backend.launch_project(project_id)

    def closeEvent(self, event):
        try:
            self._owned_backend.shutdown()
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(event)
