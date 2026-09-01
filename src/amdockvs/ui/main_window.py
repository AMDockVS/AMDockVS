from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer, QSettings, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QWidget,
)

from amdockvs.runtime import AMDockVSRuntime
from amdockvs.summaries import DockingHitSummary
from amdockvs.ui.catalog import (
    COMPLEXES_VIEW_ID,
    LIGANDS_VIEW_ID,
    RECEPTOR_VIEW_ID,
    register_binding_sites_workspace,
    register_complex_pairs_workspace,
    register_complexes_workspace,
    register_ligand_activity_workspace,
    register_ligands_workspace,
    register_molecules_workspace,
    register_receptors_workspace,
)
from amdockvs.ui.main_content import MainContentWidget
from amdockvs.ui.monitor import MONITOR_JOBS_VIEW_ID, MonitorSummaryDockWidget, register_monitor_views
from amdockvs.ui.notifications import INFO
from amdockvs.ui.projects import ApplicationWidget
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.shell.auxiliary_panel import AuxiliaryPanelController
from amdockvs.ui.shell.job_feedback import JobFeedbackController
from amdockvs.ui.shell.tool_coordinator import ToolCoordinator
from amdockvs.ui.shell.view_coordinator import ViewCoordinator
from amdockvs.ui.statusbar import StatusBar
from amdockvs.ui.tools.docking.diagram_dock import InteractionDiagramDock
from amdockvs.ui.tools.docking.studio import register_docking_workspace
from amdockvs.ui.tools.docking.grid_box import GridBoxSettingDockWidget
from amdockvs.ui.tools.molecules.build import register_build_workspace
from amdockvs.ui.tools.molecules.diversity import register_selection_workspace
from amdockvs.ui.tools.molecules.filter import register_filter_workspace
from amdockvs.ui.tools.molecules.pocket_detection import register_pocket_detection_workspace
from amdockvs.ui.tools.pymol_ribbon import install_pymol_toolbar
from amdockvs.ui.tools.qsar.chart import Glowing2DDockWidget, QSARChartDockWidget
from amdockvs.ui.tools.qsar.panels import register_qsar_panels
from amdockvs.ui.tools.workflow_panel import WORKFLOW_VIEW_ID, register_workflow_panel
from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
from amdockvs.ui.visualization.chart_controller import ChartController
from ms_components.ms_dockwidget.widget import Behavior, DockManager, MSDockWidget, Region
from ms_components.ms_pymol.widget import PymolDockWidget


def _pymol_disabled() -> bool:
    return (
            PymolDockWidget is None
            or os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen"
            or os.environ.get("AMDOCK_DISABLE_PYMOL", "").strip().lower() in {"1", "true", "yes", "on"}
    )


class AMDockVSMainWindow(QMainWindow):
    """Composition root: builds the docks, wires the signals and delegates the behaviour
    to the shell controllers (views / tools / auxiliary / jobs) and the viewer + charts.

    The delegating methods below are the window's public surface — widgets and tests call
    them on the window, not on the controllers."""

    job_submitted = Signal(str, str)  # (job name, job id) — emitted from any thread

    def __init__(self, *, runtime: AMDockVSRuntime, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.runtime = runtime
        self.monitor_bridge = runtime.create_monitor_bridge(poll_ms=500, max_recent_jobs=50)
        self._closing = False
        self._settings = QSettings()

        self.views = ViewCoordinator(self)
        self.tools = ToolCoordinator(self)
        self.aux = AuxiliaryPanelController(self)
        self.jobs = JobFeedbackController(self)
        self.viewer = MolecularViewerController(self)
        self.charts = ChartController(self)

        self.setDockOptions(
            QMainWindow.AllowNestedDocks
            | QMainWindow.AllowTabbedDocks
            | QMainWindow.AnimatedDocks
        )

        self._app_widget = None

        self._initial_docks_sized = False
        self._pymol_sized = False
        self._last_sized_width = 0  # see resizeEvent: breaks the resize/resizeDocks ping-pong
        self.dock_manager = DockManager(self)
        self.pymol_dock = None
        self.grid_dock = None
        if not _pymol_disabled():
            self.pymol_dock = PymolDockWidget("PyMOL", self.dock_manager, self)
            self.pymol_dock.resize(300, 100)
            self.pymol_dock.visibilityChanged.connect(self._on_pymol_visibility)
            self.dock_manager.add_dock(
                self.pymol_dock,
                dock_id="pymol",
                region=Region.RIGHT_TOP,
                order=10,
                behavior=Behavior.EXCLUSIVE,
                icon=load_icon("pymol.svg"),
                starts_visible=False,
            )
            self.grid_dock = GridBoxSettingDockWidget(runtime=runtime, parent=self.pymol_dock)
            self.grid_dock.setMinimumWidth(220)
            self.grid_dock.set_auto_preview_enabled(False)
            self.pymol_dock.set_side_panel(self.grid_dock, title="Grid Box", visible=False)

        # 2D interaction diagram: UNDER PyMOL, not sharing its slot — the 3D pose and its 2D
        # contact map are read together, so both are visible at once.
        self.diagram_dock = InteractionDiagramDock("2D Interactions", self.dock_manager, runtime=runtime, parent=self)
        self.dock_manager.add_dock(
            self.diagram_dock,
            dock_id="diagram",
            region=Region.RIGHT_BOTTOM,
            order=10,
            behavior=Behavior.EXCLUSIVE,
            icon=load_icon("complexes.svg"),
            starts_visible=False,
        )

        # QSAR activity-distribution chart: shares PyMOL's region (EXCLUSIVE), so activating it
        # hides PyMOL and vice versa.
        self.qsar_chart_dock = QSARChartDockWidget("Distribution", self.dock_manager, self)
        self.dock_manager.add_dock(
            self.qsar_chart_dock,
            dock_id="qsar_chart",
            region=Region.RIGHT_TOP,
            order=11,
            behavior=Behavior.EXCLUSIVE,
            icon=load_icon("activity.svg"),
            starts_visible=False,
        )
        self.qsar_chart_dock.universeHovered.connect(self.charts.on_universe_hover)
        # Glowing molecule (RDKit similarity map): also shares PyMOL's region.
        self.qsar_glow_dock = Glowing2DDockWidget("Glowing molecule", self.dock_manager, self)
        self.dock_manager.add_dock(
            self.qsar_glow_dock,
            dock_id="qsar_glow",
            region=Region.RIGHT_TOP,
            order=12,
            behavior=Behavior.EXCLUSIVE,
            icon=load_icon("activity.svg"),
            starts_visible=False,
        )

        # Left panel that hosts a tool's config UI beside the central catalog tables:
        # picking a tool from the MolTools/Docking menus mounts it here (one at a time)
        # instead of opening a central tab. See ToolCoordinator.
        self.tools_dock = MSDockWidget("Tools", self.dock_manager, self)
        self.tools_dock.setWidget(QWidget())  # placeholder; DockManager.build needs a widget
        self.dock_manager.add_dock(
            self.tools_dock,
            dock_id="tools",
            region=Region.LEFT_BOTTOM,
            order=1,
            behavior=Behavior.EXCLUSIVE,
            icon=load_icon("mol_tools.svg"),
            starts_visible=False,
        )
        self.tools_dock.visibilityChanged.connect(self.tools.on_tools_dock_visibility)

        self.monitor_dock = MonitorSummaryDockWidget("Jobs", self.dock_manager, bridge=self.monitor_bridge, parent=self)
        self.monitor_dock.open_requested.connect(self.open_jobs_monitor)
        self.dock_manager.add_dock(
            self.monitor_dock,
            dock_id="monitor",
            region=Region.BOTTOM_RIGHT,
            order=10,
            behavior=Behavior.EXCLUSIVE,
            icon=load_icon("cpu.svg"),
            starts_visible=False,  # lives in the status bar; opened on demand as a tab
        )
        # Bottom docks (Details, Jobs) stack at the bottom of the side columns instead of a
        # full-width bottom strip, so the central content view keeps the window's full height.
        self.dock_manager.set_bottom_in_lateral(True)
        self.dock_manager.build()
        # DockManager rebuilds the right column first; resize it afterwards so activating the
        # complementary 2D view starts with the same height as PyMOL.
        self.dock_manager.buttons["diagram"].toggled.connect(self._on_diagram_toggled)
        self.monitor_bridge.start()
        self.monitor_bridge.request_refresh()

        self.central_widget = MainContentWidget()
        self.central_widget.open_project_requested.connect(self._open_projects_browser)
        register_monitor_views(self)
        register_molecules_workspace(self)
        register_build_workspace(self)
        register_filter_workspace(self)
        register_selection_workspace(self)
        register_pocket_detection_workspace(self)
        register_docking_workspace(self)
        register_qsar_panels(self)
        register_workflow_panel(self)
        install_pymol_toolbar(self)
        register_ligands_workspace(self)
        register_receptors_workspace(self)
        register_binding_sites_workspace(self)
        register_complex_pairs_workspace(self)
        register_complexes_workspace(self)
        register_ligand_activity_workspace(self)
        self.central_widget.current_view_changed.connect(self._on_current_view_changed)
        self.setCentralWidget(self.central_widget)
        self.monitor_bridge.job_finished.connect(self.jobs.on_job_finished)
        self.monitor_bridge.project_changed.connect(self.jobs.on_monitor_project_changed)
        self.monitor_bridge.project_snapshot_updated.connect(self.jobs.on_project_snapshot_updated)
        self._status_bar = StatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_bar.jobs_indicator.clicked.connect(self.open_jobs_monitor)
        self._status_bar.workflow_indicator.clicked.connect(lambda: self.open_or_focus_view(WORKFLOW_VIEW_ID))
        self._status_bar.project_indicator.clicked.connect(self._show_project_summary)
        self._status_bar.resource_indicator.clicked.connect(self.open_jobs_monitor)
        self._status_bar.backend_indicator.clicked.connect(self.open_jobs_monitor)
        self.views.wire_workflow_quick_access()
        self.tools.build_tool_actions()
        self.views.build_catalog_toolbar()
        for _open_id in tuple(getattr(self.central_widget, "_open_tabs", {}).keys()):
            self.views.sync_catalog_action(str(_open_id), True)
        self.central_widget.view_open_state_changed.connect(self._on_view_open_state_changed)
        # "Job started" feedback: runtime.submit_job may be called from a worker thread, so it
        # goes through a signal (queued) before touching the UI.
        self.job_submitted.connect(self.jobs.on_job_submitted_notice)
        self.runtime.on_job_submitted = self.job_submitted.emit
        # Set window title when project is loaded
        self._sync_window_title()

        self._create_menu_bar()
        # Land on a view when a project is already open (CLI --project-id/-path); otherwise the
        # central welcome screen stays up and the user opens a project from there or the File menu.
        self._set_project_ui_enabled(self._has_active_project())
        if self._has_active_project():
            self._open_default_views()

    def _open_default_views(self) -> None:
        """Land on Receptors + Ligands: both start empty and offer their Import button,
        so a fresh project has its first step in front of the user. Molecules is the
        union view — nothing is imported *as* a molecule, so it's a poor landing tab."""
        # open_view() only returns the tab if it already exists; opening is open_or_focus_view().
        self.open_or_focus_view(LIGANDS_VIEW_ID)
        self.open_or_focus_view(RECEPTOR_VIEW_ID)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # showMaximized() shows the window at an intermediate size first, then delivers the
        # real maximized geometry through a *later* resizeEvent. Sizing on the first resize
        # locks the thirds against that intermediate width (PyMOL ends up a narrow strip), so
        # re-apply on every resize until we're actually maximized, then lock. Deferred via
        # singleShot so resizeDocks runs after this resize's own layout pass instead of being
        # overwritten by it.
        if self._initial_docks_sized or self.width() <= 400:
            return
        if self.isMaximized():
            self._initial_docks_sized = True
        # resizeDocks re-propagates size hints, and a window manager that answers with another
        # resize puts us straight back here: without the "only when the width actually changed"
        # guard that is an endless 0ms-timer loop that pegs the GUI thread (the window never
        # maximizes, so the flag above never latches).
        if self.width() == self._last_sized_width:
            return
        self._last_sized_width = self.width()
        QTimer.singleShot(0, self._apply_initial_dock_sizes)

    def _on_pymol_visibility(self, visible: bool) -> None:
        """resizeDocks is a no-op on a hidden dock, so a PyMOL that starts hidden (or is
        toggled off before the window maximizes) never gets its third and comes back at its
        old narrow width — narrow enough that the Grid Box side panel eats the whole viewer.
        Claim the third the first time it is actually on screen."""
        if visible and not self._pymol_sized and self.width() > 400:
            QTimer.singleShot(0, self._apply_initial_dock_sizes)

    def _on_diagram_toggled(self, checked: bool) -> None:
        if checked:
            QTimer.singleShot(0, self._split_viewer_docks_evenly)

    def _split_viewer_docks_evenly(self) -> None:
        pymol = self.pymol_dock
        diagram = self.diagram_dock
        if (
            pymol is None
            or not pymol.isVisible()
            or not diagram.isVisible()
            or pymol.isFloating()
            or diagram.isFloating()
        ):
            return
        self.resizeDocks([pymol, diagram], [1, 1], Qt.Vertical)

    def _third_width(self) -> int:
        return max(1, self.width() // 3)

    def _apply_initial_dock_sizes(self) -> None:
        # Left (tools) · center · right (PyMOL) ≈ equal thirds. Tools starts hidden, so it
        # gets sized to a third when it opens (see ToolCoordinator.open_tool); here we set
        # the right dock. These are hints — every dock stays user-resizable afterwards.
        try:
            if self.pymol_dock is not None and self.pymol_dock.isVisible():
                self.resizeDocks([self.pymol_dock], [self._third_width()], Qt.Horizontal)
                self._pymol_sized = True
            if self.monitor_dock is not None:
                self.resizeDocks([self.monitor_dock], [150], Qt.Vertical)
        except Exception:
            pass

    def _create_menu_bar(self):
        """Plain QMenuBar: File (Projects/Settings/Exit), View (Catalog Bar), Help.

        Replaces the NeoRibbon backstage button + quick-access bar. ApplicationWidget is
        now a hidden controller that owns the Projects/Settings dialogs."""
        from PySide6.QtCore import QSize, Qt, QUrl
        from PySide6.QtGui import QActionGroup, QDesktopServices
        from PySide6.QtWidgets import QHBoxLayout, QMenu, QToolButton

        from ms_components.theme import THEMES

        from amdockvs.ui.theme import saved_theme_name, set_theme

        self.setWindowIcon(load_icon("logo.svg"))

        self._app_widget = ApplicationWidget(runtime=self.runtime, parent=self)
        self._app_widget.project_requested.connect(self._on_application_project_requested)
        self._app_widget.settings_saved.connect(self._on_app_settings_saved)
        self._app_widget.exit_requested.connect(self.close)

        menu_bar = self.menuBar()
        # A bit taller / more breathing room so the menu bar reads as present, not incidental.
        # Padding/weight only — colors stay with the active theme.
        menu_bar.setStyleSheet(
            "QMenuBar::item { padding: 7px 14px; }"
            "QMenuBar { font-weight: 600; }"
        )

        from amdockvs.ui.catalog.molecules import import_ligands_from_file, import_receptors_from_file

        file_menu = menu_bar.addMenu("&File")
        file_menu.addAction(load_icon("catalog.svg"), "Projects…", self._app_widget.open_projects)
        file_menu.addSeparator()
        self._project_actions = [
            file_menu.addAction(load_icon("ligands.svg"), "Import Ligands…",
                                lambda: import_ligands_from_file(self)),
            file_menu.addAction(load_icon("receptor.svg"), "Import Receptors…",
                                lambda: import_receptors_from_file(self)),
        ]
        file_menu.addSeparator()
        file_menu.addAction(load_icon("info.svg"), "Settings…", self._app_widget.open_settings)
        file_menu.addAction("Exit", self.close)

        view_menu = menu_bar.addMenu("&View")
        toolbars_menu = view_menu.addMenu("Toolbars")
        cgrp = QActionGroup(self)
        cgrp.setExclusive(True)
        current_style = self.views.saved_toolbar_button_style()
        for key, (label, *_rest) in self.views.TOOLBAR_BUTTON_STYLES.items():
            act = toolbars_menu.addAction(label)
            act.setCheckable(True)
            act.setData(key)
            act.setChecked(key == current_style)
            cgrp.addAction(act)
        toolbars_menu.triggered.connect(lambda a: self.views.set_toolbar_button_style(a.data()))

        help_menu = menu_bar.addMenu("&Help")
        help_menu.addAction(load_icon("doc.svg"), "Documentation",
                            lambda: QDesktopServices.openUrl(QUrl(self._DOCS_URL)))
        help_menu.addAction(load_icon("info.svg"), "About AMDockVS", self._show_about)

        # Theme: compact button pinned to the menu bar's top-right corner (global, always
        # visible, out of the File/Help flow). Replaces the old View > Theme submenu.
        theme_btn = QToolButton(self)
        theme_btn.setObjectName("menuBarTheme")
        theme_btn.setIcon(load_icon("theme.svg"))
        theme_btn.setToolTip("Theme")
        theme_btn.setAutoRaise(True)
        theme_btn.setPopupMode(QToolButton.InstantPopup)
        # Keep the menu bar at text height: fill the button with the icon, no popup arrow,
        # no internal/menu-bar padding.
        theme_btn.setIconSize(QSize(20, 20))
        theme_btn.setFixedSize(QSize(22, 22))
        theme_btn.setStyleSheet(
            "QToolButton { padding: 0; margin: 0; border: none; background: transparent; }"
            "QToolButton::menu-indicator { image: none; }"
        )
        theme_menu = QMenu(theme_btn)
        tgrp = QActionGroup(self)
        tgrp.setExclusive(True)
        current = saved_theme_name()
        for name in ("auto", *THEMES):
            act = theme_menu.addAction(name.replace("_", " ").title())
            act.setCheckable(True)
            act.setData(name)
            act.setChecked(name == current)
            tgrp.addAction(act)
        theme_menu.triggered.connect(lambda a: set_theme(a.data(), self, QApplication.instance()))
        theme_btn.setMenu(theme_menu)

        corner = QWidget(self)
        corner_row = QHBoxLayout(corner)
        corner_row.setContentsMargins(0, 0, 6, 0)
        corner_row.setSpacing(10)
        # Bell next to the theme button: global, always visible, and its drop-down IS the log.
        corner_row.addWidget(self.jobs.build_bell())
        corner_row.addWidget(theme_btn)
        menu_bar.setCornerWidget(corner, Qt.TopRightCorner)

    def open_settings(self) -> None:
        """Open the Settings dialog (feature panels point here for tool installs)."""
        if self._app_widget is not None:
            self._app_widget.open_settings()

    def _on_app_settings_saved(self) -> None:
        # Refresh what's open; saving settings shouldn't pop tabs the user didn't ask for.
        self.views.refresh_open_views_once()
        # Saved worker settings only mean something once they are registered. Cheap
        # executors are re-registered here; ray stays for the monitor to activate
        # (it may need a cluster launch), so this never blocks on the network.
        if self._has_active_project():
            try:
                self.runtime.molsuite.reload_configured_executors()
            except Exception as exc:  # noqa: BLE001 - a bad executor config must not eat the save
                self.statusBar().showMessage(f"Executors not applied: {exc}", 8000)

    _DOCS_URL = "https://github.com/Valdes-Tresanco-MS/AMDock"

    def _show_about(self):
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.about(
            self,
            "About AMDockVS",
            "AMDockVS\n\nVirtual-screening front-end over AMDock + MolSuite.",
        )

    def _has_active_project(self) -> bool:
        return getattr(self.runtime, "active_context", None) is not None

    def _set_project_ui_enabled(self, on: bool) -> None:
        """Without an open project only the welcome screen, the File menu and Settings work.

        Gating the entry points is why the views need no "no project" state of their own:
        nothing can build one. ponytail: one-way switch — active_context never goes back to
        None (another project opens a new process, see _on_application_project_requested);
        make it a signal the day a project can be closed in place.
        """
        from PySide6.QtWidgets import QToolBar

        for bar in self.findChildren(QToolBar):  # data toolbar + PyMOL bar + dock/tool buttons
            bar.setEnabled(on)
        if self.dock_manager is not None:
            for entry in self.dock_manager.entries.values():  # docks already on screen
                entry.dock.setEnabled(on)
        self._status_bar.setEnabled(on)
        for action in self._project_actions:
            action.setEnabled(on)

    def _open_projects_browser(self) -> None:
        if self._app_widget is not None and not self._closing:
            self._app_widget.open_projects()

    # -- views / tools / auxiliary (delegating to the shell controllers) -----------

    _TOOL_VIEW_IDS = ViewCoordinator.TOOL_VIEW_IDS
    _STANDING_DATA_VIEWS = ViewCoordinator.STANDING_DATA_VIEWS
    _TOOL_ACTIONS = ToolCoordinator.TOOL_ACTIONS
    _TOOL_AUX_VIEWS = AuxiliaryPanelController.TOOL_AUX_VIEWS
    _AUX_DETAILS = AuxiliaryPanelController.AUX_DETAILS

    def register_main_view(self, view_id: str, title: str, factory, *, on_close=None) -> None:
        self.views.register_main_view(view_id, title, factory, on_close=on_close)

    def open_or_focus_view(self, view_id: str) -> QWidget:
        return self.views.open_or_focus_view(view_id)

    def open_view(self, view_id: str) -> QWidget | None:
        return self.views.open_view(view_id)

    def open_tool(self, view_id: str) -> QWidget:
        return self.tools.open_tool(view_id)

    def aux_view(self, view_id: str) -> QWidget | None:
        return self.aux.page_for(view_id)

    def show_catalog_selection_details(self, kind: str, obj) -> None:
        self.aux.show_catalog_selection_details(kind, obj)

    def _set_aux_occupant(self, *args) -> None:
        self.aux.set_occupant(*args)

    def _on_current_view_changed(self, view_id: str) -> None:
        self.viewer.hide_grid_panel()
        self.aux.set_occupant()  # the tab on screen may own the auxiliary panel
        self.charts.sync_to_active_view(view_id)
        self.viewer.sync_to_active_view(view_id)
        # Switching tabs changes whether a jobs surface is on screen, so the status
        # bar indicator (the always-visible fallback) has to be re-evaluated.
        self.jobs.update_jobs_statusbar()

    def _on_view_open_state_changed(self, view_id: str, is_open: bool) -> None:
        if str(view_id) == MONITOR_JOBS_VIEW_ID:
            self.jobs.update_jobs_statusbar()
        self.views.sync_workflow_action(str(view_id), bool(is_open))
        self.views.sync_catalog_action(str(view_id), bool(is_open))

    @property
    def _active_tool(self):
        return self.tools.active_tool

    @property
    def _tool_action_buttons(self):
        return self.tools.action_buttons

    def _on_tool_action(self, view_id: str, checked: bool) -> None:
        self.tools.on_tool_action(view_id, checked)

    @property
    def _catalog_actions(self):
        return self.views.catalog_actions

    @property
    def _catalog_toolbar(self):
        return self.views.catalog_toolbar

    @property
    def _aux_occupant(self):
        return self.aux.occupant

    # -- jobs, notifications and monitor -------------------------------------------

    def open_jobs_monitor(self) -> QWidget:
        self.monitor_dock.hide()
        self._status_bar.jobs_indicator.set_attention(False)  # seen it — clear the red cue
        widget = self.open_or_focus_view(MONITOR_JOBS_VIEW_ID)
        self.jobs.update_jobs_statusbar()
        return widget

    def open_complex_results(self) -> QWidget:
        return self.open_or_focus_view(COMPLEXES_VIEW_ID)

    def restore_monitor_dock(self) -> None:
        self.monitor_dock.show()
        self.jobs.update_jobs_statusbar()

    def post_notification(self, title: str, text: str = "", level: str = INFO) -> None:
        self.jobs.post_notification(title, text, level)

    def open_notifications(self) -> None:
        self.jobs.open_notifications()

    @property
    def _notifications(self):
        return self.jobs.notifications

    @property
    def _notification_bell(self):
        return self.jobs.notification_bell

    @property
    def _rows_loaded_views(self):
        return self.jobs.rows_loaded_views

    @property
    def _view_refresh_timer(self):
        return self.jobs.view_refresh_timer

    _summarize_failure_message = staticmethod(JobFeedbackController._summarize_failure_message)

    def _on_project_snapshot_updated(self, snapshot) -> None:
        self.jobs.on_project_snapshot_updated(snapshot)

    def _refresh_current_view_in_background(self) -> None:
        self.jobs.refresh_current_view_in_background()

    # -- PyMOL viewer ---------------------------------------------------------------

    def load_hit_in_pymol(self, hit: DockingHitSummary, pose_rank: int = 1) -> None:
        self.viewer.load_hit(hit, pose_rank)
        # The 2D diagram follows the same selection; it only reads a file path, so this is free
        # even when the dock is hidden.
        self.diagram_dock.show_hit(hit, pose_rank)

    def _load_hit_in_pymol(self, hit: DockingHitSummary, pose_rank: int = 1) -> None:
        self.viewer.load_hit(hit, pose_rank)

    def focus_receptor_in_pymol(self, receptor) -> None:
        self.viewer.focus_receptor_in_pymol(receptor)

    def highlight_receptor_residue(self, receptor_id: int, chain: str, resnum: int) -> None:
        self.viewer.highlight_receptor_residue(receptor_id, chain, resnum)

    def _show_binding_site_from_details(self, molecule, site) -> None:
        # Called by getattr() from the pocket table and the binding-sites view.
        self.viewer.show_binding_site(molecule, site)

    # -- charts ---------------------------------------------------------------------

    def show_activity_histogram(self, endpoint: str, bins) -> None:
        self.charts.show_activity_histogram(endpoint, bins)

    def show_feature_importance(self, label: str, pairs) -> None:
        self.charts.show_feature_importance(label, pairs)

    def show_roc_curves(self, curves) -> None:
        self.charts.show_roc_curves(curves)

    def show_model_fit_series(self, series) -> None:
        self.charts.show_model_fit_series(series)

    def show_correlation_heatmap(self, label: str, labels, matrix) -> None:
        self.charts.show_correlation_heatmap(label, labels, matrix)

    def show_split_distribution(self, label: str, categories, groups) -> None:
        self.charts.show_split_distribution(label, categories, groups)

    def show_similarity_distribution(self, label: str, bins) -> None:
        self.charts.show_similarity_distribution(label, bins)

    def show_size_distribution(self, label: str, labels, counts) -> None:
        self.charts.show_size_distribution(label, labels, counts)

    def show_diversity_universe(
            self, pool_points, selection_groups, evr=(0.0, 0.0), highlight_points=None
    ) -> None:
        self.charts.show_diversity_universe(pool_points, selection_groups, evr, highlight_points)

    def show_glowing_molecule(self, molblock: str, weights, caption: str = "") -> None:
        self.charts.show_glowing_molecule(molblock, weights, caption)

    # -- project lifecycle ----------------------------------------------------------

    def _show_project_summary(self) -> None:
        from amdockvs.ui.project_summary import show_project_summary
        show_project_summary(self)

    def _sync_window_title(self) -> None:
        active_context = getattr(self.runtime, "active_context", None)
        if active_context is None:
            self.setWindowTitle("AMDockVS")
            self._status_bar.project_indicator.set_project(None)
            return
        self.setWindowTitle(f"AMDockVS - {active_context.name}")
        self._status_bar.project_indicator.set_project(active_context.name)

    def _on_application_project_requested(self, project_id: str) -> None:
        if self._app_widget is not None:
            self._app_widget.hide()
        active_context = getattr(self.runtime, "active_context", None)
        if active_context is not None and str(active_context.id) == str(project_id):
            return
        if active_context is None:
            try:
                self.runtime.open_project(project_id)
            except Exception as exc:
                QMessageBox.critical(self, "Open Project", f"Could not open the selected project:\n{exc}")
                return
            self.monitor_bridge.request_refresh()
            self._sync_window_title()
            self._set_project_ui_enabled(True)
            self.aux.reset()
            self._open_default_views()
            return
        reply = QMessageBox.question(
            self,
            "Open Project",
            "The selected project will open in a fresh AMDock window. The current window will close. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            process = self._app_widget.launch_project(project_id)
        except Exception as exc:
            QMessageBox.critical(self, "Open Project", f"Could not launch the selected project:\n{exc}")
            return
        if process.poll() is not None:
            QMessageBox.critical(self, "Open Project", "The new AMDock process exited before initialization.")
            return
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        self.runtime.on_job_submitted = None  # the toast's window is going away
        self.jobs.stop_timers()
        # Native detached docks have no QObject parent by design so the OS treats
        # them as independent, snappable windows. Reattach them before teardown so
        # their widgets (especially PyMOL/OpenGL) are destroyed with this window.
        self.dock_manager.close_windowed_docks()
        try:
            self.monitor_bridge.stop()
        finally:
            if self._app_widget is not None:
                self._app_widget.close()
            super().closeEvent(event)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = AMDockVSMainWindow(runtime=AMDockVSRuntime())
    window.showMaximized()
    sys.exit(app.exec())
