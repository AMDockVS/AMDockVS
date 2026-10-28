from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer, QSettings, QSignalBlocker, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QHBoxLayout,
    QMessageBox,
    QWidget,
)

from amdockvs.core.home import amdock_home
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.project.summaries import DockingHitSummary
from amdockvs.ui.catalog.common import SelectionStore
from amdockvs.ui.shell.main_content import MainContentWidget
from amdockvs.ui.shell.tutorials_panel import TutorialsPanel
from amdockvs.ui.monitor import JobsDialog
from amdockvs.ui.shell.projects import ApplicationWidget
from amdockvs.ui.registry import register_all
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.shell.auxiliary_panel import AuxiliaryPanelController
from amdockvs.ui.shell.job_feedback import JobFeedbackController
from amdockvs.ui.shell.tool_coordinator import ToolCoordinator
from amdockvs.ui.shell.view_coordinator import ViewCoordinator
from amdockvs.ui.shell.statusbar import StatusBar
from amdockvs.ui.tools.docking.diagram_dock import InteractionDiagramDock
from amdockvs.ui.tools.binding_sites.box import GridBoxSettingDockWidget
from amdockvs.ui.tools.qsar.chart import Glowing2DDockWidget, QSARChartDockWidget
from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
from amdockvs.ui.visualization.chart_controller import ChartController
from ms_components.ms_dockwidget.widget import Behavior, DockManager, MSDockWidget, Region
from ms_components.ms_executors import ExecutorsPanel
from ms_components.ms_pymol.widget import PymolDockWidget
from amdockvs.ui.view_ids import (
    COMPLEX_PAIRS_VIEW_ID,
    COMPLEXES_VIEW_ID,
    LIGANDS_VIEW_ID,
    OFFTARGET_VIEW_ID,
    RECEPTOR_VIEW_ID,
    REDOCKING_VIEW_ID,
    SHARDS_VIEW_ID,
    WORKFLOW_VIEW_ID,
)

# Views whose rows are docked poses: the 2D interaction diagram only has something to show there.
_RESULT_VIEW_IDS = frozenset({COMPLEXES_VIEW_ID, OFFTARGET_VIEW_ID, REDOCKING_VIEW_ID})


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
    import_dialog_opened = Signal(str, object)
    project_opened = Signal(str)

    def __init__(self, *, runtime: AMDockVSRuntime, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.runtime = runtime
        self.monitor_bridge = runtime.create_monitor_bridge(poll_ms=500, max_recent_jobs=50)
        self.jobs_dialog: JobsDialog | None = None  # built on first open_jobs_monitor()
        self.executors_panel: ExecutorsPanel | None = None  # built on first open_executors_panel()
        self._closing = False
        self._diagram_parked = False  # hidden by leaving the results views, reopens on return
        # A file of ours, not Qt's per-organization place: it goes when the home goes.
        self._settings = QSettings(str(amdock_home() / "ui_state.ini"), QSettings.Format.IniFormat)

        self.views = ViewCoordinator(self)
        self.tools = ToolCoordinator(self)
        self.selection = SelectionStore(self)  # what "Select" picked, shared by tables and tools
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
        # contact map are read together, so both are visible at once. BOTTOM_RIGHT with
        # bottom-in-lateral lands it at the foot of the right column, and its button at the
        # foot of the right bar, level with where the panel opens.
        self.diagram_dock = InteractionDiagramDock("2D Interactions", self.dock_manager, runtime=runtime, parent=self)
        self.diagram_dock.settings_requested.connect(lambda node: self.open_settings(node=node))
        self.diagram_dock.residue_selected.connect(lambda *pick: self.viewer.show_diagram_residue(*pick))
        self.diagram_dock.diagram_built.connect(self._on_dock_diagram_built)
        self.dock_manager.add_dock(
            self.diagram_dock,
            dock_id="diagram",
            region=Region.BOTTOM_RIGHT,
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
        # picking a tool shows it here (one at a time) instead of opening a central tab.
        # See ToolCoordinator.
        self.tools_dock = MSDockWidget("Tools", self.dock_manager, self)
        self.tools_dock.setWidget(self.tools.stack)  # one page per tool, built on first open
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

        # Bottom docks (2D Interactions) stack at the bottom of the side columns instead of a
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
        hide_tutorials = self._settings.value("tutorials/hide_on_startup", False, type=bool)
        self.tutorials_panel = TutorialsPanel(hide_on_startup=hide_tutorials)
        self.tutorials_panel.start_requested.connect(self._start_docking_tutorial)
        self.tutorials_panel.close_requested.connect(lambda: self._set_tutorials_panel_visible(False))
        self.tutorials_panel.startup_preference_changed.connect(self._set_tutorials_startup_preference)
        register_all(self)
        self.central_widget.current_view_changed.connect(self._on_current_view_changed)
        central_shell = QWidget(self)
        shell_layout = QHBoxLayout(central_shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.tutorials_panel)
        shell_layout.addWidget(self.central_widget, 1)
        self.tutorials_panel.setVisible(not hide_tutorials)
        self.setCentralWidget(central_shell)
        self.monitor_bridge.job_finished.connect(self.jobs.on_job_finished)
        self.monitor_bridge.project_changed.connect(self.jobs.on_monitor_project_changed)
        self.monitor_bridge.project_snapshot_updated.connect(self.jobs.on_project_snapshot_updated)
        self._status_bar = StatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_bar.jobs_indicator.clicked.connect(self.open_jobs_monitor)
        for role, indicator in self._status_bar.selection_indicators.items():
            indicator.clicked.connect(lambda role=role: self.selection.set(role, None))
        self.selection.changed.connect(lambda role: self._status_bar.selection_indicators[role].set_count(
            len(self.selection.get(role) or ())))
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
        union view — nothing is imported *as* a molecule, so it's a poor landing tab. A project
        with shards also opens Shards: that is where its screening library is."""
        # open_view() only returns the tab if it already exists; opening is open_or_focus_view().
        self.open_or_focus_view(RECEPTOR_VIEW_ID)
        self.open_or_focus_view(LIGANDS_VIEW_ID)
        if self.views._library_shape()[0]:
            self.open_or_focus_view(SHARDS_VIEW_ID)
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

        from amdockvs.ui.common.theme import saved_theme_name, set_theme

        self.setWindowIcon(load_icon("logo.svg"))

        self._app_widget = ApplicationWidget(runtime=self.runtime, parent=self)
        self._app_widget.project_requested.connect(self._on_application_project_requested)
        self._app_widget.active_project_delete_requested.connect(self._on_active_project_delete_requested)
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
        self.tutorials_panel_action = view_menu.addAction("Tutorials panel")
        self.tutorials_panel_action.setCheckable(True)
        self.tutorials_panel_action.setChecked(not self.tutorials_panel.isHidden())
        self.tutorials_panel_action.toggled.connect(self._set_tutorials_panel_visible)
        view_menu.addSeparator()
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

        def guided_tour():
            from amdockvs.ui.tutorials import start_interface_tour

            start_interface_tour(self)

        help_menu.addAction("Guided tour…", guided_tour)
        help_menu.addAction(load_icon("web.svg"), "Web Site",
                            lambda: QDesktopServices.openUrl(QUrl(self._WEB_URL)))
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

    def open_settings(self, *, node: str | None = None) -> None:
        """Open the Settings dialog, on ``node`` when a feature panel points at its own page."""
        if self._app_widget is not None:
            self._app_widget.open_settings(node=node)

    def _on_app_settings_saved(self) -> None:
        # Refresh what's open; saving settings shouldn't pop tabs the user didn't ask for.
        self.views.refresh_open_views_once()
        self.diagram_dock.apply_settings()

    _DOCS_URL = "https://amdockvs.github.io/AMDockVS"  # AMDockVS docs; the old link pointed at AMDock 1.6.x
    _WEB_URL = "https://amdockvs.github.io"  # AMDockVS docs; the old link pointed at AMDock 1.6.x

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
        None (another project hot-restarts the app, see _on_application_project_requested);
        make it a signal the day a project can be closed in place.
        """
        from PySide6.QtWidgets import QToolBar

        # data toolbar + PyMOL bar + dock/tool buttons, the docks already on screen, the status bar
        widgets = [*self.findChildren(QToolBar), self._status_bar]
        if self.dock_manager is not None:
            widgets += [entry.dock for entry in self.dock_manager.entries.values()]
        if not on:  # remember what would show, so a bar the user hid stays hidden later
            self._shown_with_project = [w for w in widgets if w.isVisibleTo(self)]
        for widget in widgets:
            widget.setEnabled(on)
        for widget in getattr(self, "_shown_with_project", ()):
            widget.setVisible(on)  # hidden, not greyed: the welcome screen stands alone
        for action in self._project_actions:
            action.setEnabled(on)
        # The tutorials library belongs to the welcome page; the guided
        # tutorial itself continues in the project workspace without it.
        if on:
            self._set_tutorials_panel_visible(False)
        self.tutorials_panel_action.setEnabled(not on)

    def _open_projects_browser(self) -> None:
        if self._app_widget is not None and not self._closing:
            self._app_widget.open_projects()

    def _start_docking_tutorial(self) -> None:
        from amdockvs.ui.docking_tutorial import launch_simple_docking_tutorial

        launch_simple_docking_tutorial(self)

    def _set_tutorials_panel_visible(self, visible: bool) -> None:
        self.tutorials_panel.setVisible(visible)
        action = getattr(self, "tutorials_panel_action", None)
        if action is not None:
            with QSignalBlocker(action):
                action.setChecked(visible)

    def _set_tutorials_startup_preference(self, hide: bool) -> None:
        self._settings.setValue("tutorials/hide_on_startup", hide)
        self._settings.sync()
        if hide:
            self._set_tutorials_panel_visible(False)

    # -- views / tools / auxiliary (delegating to the shell controllers) -----------

    def register_main_view(self, view_id: str, title: str, factory, *, on_close=None) -> None:
        self.views.register_main_view(view_id, title, factory, on_close=on_close)

    def open_or_focus_view(self, view_id: str) -> QWidget:
        return self.views.open_or_focus_view(view_id)

    def show_catalog_selection_details(self, kind: str, obj) -> None:
        self.aux.show_catalog_selection_details(kind, obj)

    def show_record(self, kind: str, record_id: int, *, reveal: bool = False) -> None:
        """A receptor/ligand/complex named in another table: its details in a window, or
        (reveal) its row in its own catalog tab, shown alone until "Show all"."""
        api = self.runtime.complexes if kind == "complex" else self.runtime.molecules
        record = api.get(int(record_id))  # one row by primary key, cheap on the GUI thread
        if record is None:
            return
        if not reveal:
            self.aux.open_details_window(kind, record)
            return
        view_id = {"complex": COMPLEX_PAIRS_VIEW_ID, "receptor": RECEPTOR_VIEW_ID}.get(kind, LIGANDS_VIEW_ID)
        self.open_or_focus_view(view_id).reveal([record.id], label=getattr(record, "name", None) or f"{kind} {record.id}")

    def _on_current_view_changed(self, view_id: str) -> None:
        self.viewer.hide_grid_panel()
        self.aux.set_occupant()  # the tab on screen may own the auxiliary panel
        self.charts.sync_to_active_view(view_id)
        self.viewer.sync_to_active_view(view_id)
        self._sync_diagram_to_view(view_id)
        # Switching tabs changes whether a jobs surface is on screen, so the status
        # bar indicator (the always-visible fallback) has to be re-evaluated.
        self.jobs.update_jobs_statusbar()

    def _on_dock_diagram_built(self, result_id: int) -> None:
        # Selected Result of whichever results page shows that pose lists its new interactions.
        from amdockvs.ui.catalog.results import DockingResultsWidget

        for page in self.findChildren(DockingResultsWidget):
            page.diagram_saved(result_id)

    def _sync_diagram_to_view(self, view_id: str) -> None:
        """The 2D diagram follows the results views: it hides when the user leaves them and
        comes back on return if it was open. Closing it by hand on another tab is respected."""
        # ponytail: experimental complexes have no diagram of their own yet; when they do, a
        # view showing one joins _RESULT_VIEW_IDS.
        on_results = str(view_id) in _RESULT_VIEW_IDS
        if not on_results and self.dock_manager.buttons["diagram"].isChecked():
            self._diagram_parked = True
            self.dock_manager.toggle("diagram", False)
        elif on_results and self._diagram_parked:
            self._diagram_parked = False
            self.dock_manager.toggle("diagram", True)

    def _on_view_open_state_changed(self, view_id: str, is_open: bool) -> None:
        self.views.sync_workflow_action(str(view_id), bool(is_open))
        self.views.sync_catalog_action(str(view_id), bool(is_open))

    # -- jobs, notifications and monitor -------------------------------------------

    def open_jobs_monitor(self) -> JobsDialog:
        """One non-modal Jobs window: raised if already open, built once, hidden on close."""
        if self.jobs_dialog is None:
            self.jobs_dialog = JobsDialog(bridge=self.monitor_bridge, parent=self)
            self.jobs_dialog.finished.connect(lambda _result: self.jobs.update_jobs_statusbar())
        self._status_bar.jobs_indicator.set_attention(False)  # seen it — clear the red cue
        self.jobs_dialog.show()
        self.jobs_dialog.raise_()
        self.jobs_dialog.activateWindow()
        self.jobs.update_jobs_statusbar()
        return self.jobs_dialog

    def open_executors_panel(self) -> ExecutorsPanel:
        """The quick window that starts and stops the executors: built once, hidden on close."""
        if self.executors_panel is None:
            self.executors_panel = ExecutorsPanel(self.runtime.workspace, parent=self)
            self.executors_panel.settings_requested.connect(lambda: self.open_settings(node="page:Executors"))
        self.executors_panel.show()
        self.executors_panel.raise_()
        self.executors_panel.activateWindow()
        return self.executors_panel

    def open_complex_results(self) -> QWidget:
        return self.open_or_focus_view(COMPLEXES_VIEW_ID)

    # -- PyMOL viewer ---------------------------------------------------------------

    def load_hit_in_pymol(self, hit: DockingHitSummary, pose_rank: int = 1) -> None:
        self.viewer.load_hit(hit, pose_rank)
        # The 2D diagram follows the same selection; it only reads a file path, so this is free
        # even when the dock is hidden.
        self.diagram_dock.show_hit(hit, pose_rank)

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

    def hide_glowing_molecule(self) -> None:
        self.charts.hide_glowing_molecule()

    # -- project lifecycle ----------------------------------------------------------

    def _show_project_summary(self) -> None:
        from amdockvs.ui.shell.project_summary import show_project_summary
        show_project_summary(self)

    def _sync_window_title(self) -> None:
        active_context = getattr(self.runtime, "active_context", None)
        if active_context is None:
            self.setWindowTitle("AMDockVS")
            self._status_bar.project_indicator.set_project(None)
            self.views.sync_catalog_availability()
            return
        self.setWindowTitle(f"AMDockVS - {active_context.name}")
        self._status_bar.project_indicator.set_project(active_context.name)
        self.views.sync_catalog_availability()

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
            self.project_opened.emit(str(project_id))
            return
        reply = QMessageBox.question(
            self,
            "Open Project",
            "AMDockVS restarts to open the selected project. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            from amdockvs.app import restart

            restart(self, "--project-id", str(project_id))

    def _on_active_project_delete_requested(self, project_id: str) -> None:
        """The projects browser confirmed deleting the open project: restart without it, then delete."""
        from amdockvs.app import restart

        restart(self, "--delete-project-id", str(project_id))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            super().closeEvent(event)
            return
        tutorial = getattr(self, "_docking_tutorial", None)
        if tutorial is not None:
            tutorial.controller.stop()
        from amdockvs.ui.tools.importing.receptors import ReceptorImportPanel

        preview = next((panel for panel in self.findChildren(ReceptorImportPanel) if panel.preview_running), None)
        if preview is not None:
            event.ignore()
            preview.preview_finished.connect(self.close, Qt.ConnectionType.SingleShotConnection)
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
    from amdockvs.app import main

    sys.exit(main())
