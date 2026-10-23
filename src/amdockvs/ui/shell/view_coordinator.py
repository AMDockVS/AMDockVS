"""Central tabs + the top data toolbar + the workflow quick-access button.

Owns "which view is open and which button shows it"; the window keeps thin delegators
(register_main_view / open_or_focus_view / open_view) because widgets call them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from amdockvs.ui.registry import (
    CATALOG_TABLES,
    CONTEXTUAL_VIEWS,
    EPHEMERAL_VIEW_IDS,
    PREVIEW_VIEW_IDS,
    STANDING_DATA_VIEWS,
    TOOL_VIEW_IDS,
)
from amdockvs.ui.resources.icons import icon as load_icon
from ms_components.ms_dockwidget.widget import Region
from amdockvs.ui.view_ids import LIGANDS_VIEW_ID, SHARDS_VIEW_ID, WORKFLOW_VIEW_ID


class ViewCoordinator:
    # View > Toolbars. key -> (menu label, Qt style, icon px, font px, side-toolbar width,
    # logo px). Applies to every toolbar: the top catalog bar and the left/right dock toolbars.
    # The logo's separator lines up with the side toolbar's edge (LOGO_INSET_PX).
    TOOLBAR_BUTTON_STYLES = {
        "icons": ("Icons Only", Qt.ToolButtonIconOnly, 24, None, 41, 19),
        "icon_text": ("Icon + Text", Qt.ToolButtonTextUnderIcon, 20, 9, 64, 26),
    }
    TOOLBAR_STYLE_KEY = "ui/toolbar_button_style"
    GROUP_GAP_PX = 10  # padding on each side of the `|` between view groups

    def __init__(self, window):
        self.w = window
        self.catalog_actions: dict[str, QAction] = {}
        self.catalog_toolbar = None
        self.contextual_chrome: tuple[QAction, ...] = ()  # gap + end separator of that group
        self.workflow_action_button = None
        self.logo = None

    # -- central tabs --------------------------------------------------------------

    def register_main_view(self, view_id: str, title: str, factory, *, on_close=None) -> None:
        self.w.central_widget.register_view(
            view_id, title, factory, on_close=on_close,
            preview=view_id in PREVIEW_VIEW_IDS,
        )

    def open_or_focus_view(self, view_id: str) -> QWidget:
        if view_id in TOOL_VIEW_IDS:
            return self.w.tools.open_tool(view_id)
        return self.w.central_widget.open_or_focus_view(view_id)

    def open_view(self, view_id: str) -> QWidget | None:
        return self.w.central_widget.open_view(view_id)

    def current_view(self):
        """(view_id, widget) of the visible tab; ("", None) when there is none."""
        view_id = self.w.central_widget.current_view_id()
        if not view_id:
            return "", None
        return str(view_id), self.w.central_widget.open_view(view_id)

    def refresh_open_views_once(self) -> None:
        open_view_ids = tuple(getattr(self.w.central_widget, "_open_tabs", {}).keys())
        for view_id in open_view_ids:
            self.w.central_widget.refresh_open_view(view_id)
        self.w.aux.refresh()

    # -- top data toolbar ----------------------------------------------------------

    def build_catalog_toolbar(self) -> None:
        """Data views as a native top toolbar of checkable actions. Checked reflects
        whether the view's central tab is open (synced both ways); clicking the current
        view closes it, any other click opens/focuses it. The pressed state is the cue.

        Sections: catalog tables | standing result views | contextual views | auxiliary-panel
        toggle. Groups sit apart behind a wide gap; the plain `|` is for the bar's chrome."""
        from PySide6.QtWidgets import QToolBar

        self.catalog_actions = {}  # view_id -> checkable QAction
        bar = QToolBar("Data", self.w)
        bar.setObjectName("catalog_toolbar")
        bar.setMovable(False)
        self.w.addToolBar(Qt.TopToolBarArea, bar)
        self.catalog_toolbar = bar
        self.logo = self._make_logo(bar)
        bar.addWidget(self.logo)
        bar.addSeparator()
        self._add_logo_separator_below()
        gap = QWidget(bar)  # the first table button keeps off the separator
        gap.setFixedWidth(self.LOGO_GAP_PX)
        bar.addWidget(gap)
        for index, entries in enumerate((CATALOG_TABLES, *STANDING_DATA_VIEWS)):
            if index:
                self._add_group_gap(bar)
            for entry in entries:
                self._add_data_action(bar, *entry)
        gap = self._add_group_gap(bar)
        for entry in CONTEXTUAL_VIEWS:
            self._add_data_action(bar, *entry)
        self.contextual_chrome = (*gap, bar.addSeparator())
        for view_id in EPHEMERAL_VIEW_IDS:  # only there while its tab is
            self.catalog_actions[view_id].setVisible(self.catalog_actions[view_id].isChecked())
        # The auxiliary-zone toggle is not a table: it floats right, past a stretch, so the
        # catalog actions never push it around.
        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        # Neutral label on purpose: the toggle owns the band, not what lands in it.
        # The action itself belongs to the auxiliary zone; the bar only hosts it.
        self.w.aux.install_action(bar.addAction(load_icon("details.svg"), "Panel"))
        # Jobs opens a window, not a region: the tools show their own progress, so the full
        # monitor is on demand. Disabled with no project like the rest of the bar.
        bar.addSeparator()
        self.jobs_action = bar.addAction(load_icon("cpu.svg"), "Jobs")
        self.jobs_action.setToolTip("Open the Jobs monitor.")
        self.jobs_action.triggered.connect(self.w.open_jobs_monitor)
        self.executors_action = bar.addAction(load_icon("cloud.svg"), "Executors")
        self.executors_action.setToolTip("Who runs the jobs: start and stop the executors.")
        self.executors_action.triggered.connect(self.w.open_executors_panel)
        self.sync_catalog_availability()
        # Every toolbar button now exists (docks + actions + these), so one pass styles them all.
        self.set_toolbar_button_style(self.saved_toolbar_button_style())

    LOGO_GAP_PX = 6  # between the logo's separator and the first table button
    # Bar margin (4) + spacing (1) + half the separator (3): its line lands on the side bar's edge.
    LOGO_INSET_PX = 8

    @staticmethod
    def _make_logo(bar) -> QPushButton:
        """Branding only: a button for the themed icon (a pixmap would freeze on theme change),
        made inert so it never hovers or takes a click."""
        from importlib.resources import files
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        from amdockvs.ui.resources.icons.themed import themed_icon

        role = QPalette.ColorRole
        # The "A" follows the theme's text colour; the molecule keeps the SVG's teal.
        # Not a QToolButton: inside a QToolBar one paints at the bar's icon size, not its own.
        logo = QPushButton(bar)
        logo.setObjectName("toolbar_logo")
        logo.setIcon(themed_icon(str(files("amdockvs.ui.resources.icons") / "logo.svg"), role.WindowText))
        logo.setFocusPolicy(Qt.NoFocus)
        logo.setAttribute(Qt.WA_TransparentForMouseEvents)
        logo.setToolTip("AMDockVS")
        # Frameless: the separators beside and below it mark it off from the bars.
        logo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        logo.setStyleSheet(
            "QPushButton#toolbar_logo { border: none; background: transparent; padding: 1px; }"
        )
        # Dimmed so it never competes with the bars' actions.
        logo.setGraphicsEffect(QGraphicsOpacityEffect(logo, opacity=0.6))
        return logo

    def _add_logo_separator_below(self) -> None:
        """A line on top of the left tools bar, under the logo's corner."""
        from ms_components.ms_dockwidget.widget import ToolbarSeparator

        if self.w.dock_manager is None:
            return
        # ponytail: reaches into the bar's outer layout; a ms_components API if it grows.
        self.w.dock_manager.left_toolbar.layout().insertWidget(0, ToolbarSeparator())

    def sync_catalog_availability(self) -> None:
        """Which catalog tables this project has anything to say with.

        Refreshed on project open and after every finished job: a shard import is what
        brings the Shards button.

        Never a rename: a table always means the same thing. Shards only exist in a project
        whose library was imported sharded, so its button is absent everywhere else. Ligands
        stays — it is the reference cocrystals (redocking, QSAR) and the hits promoted out of
        a campaign — but in a sharded project it is greyed until one of those exists, because
        the screening library is *not* in it and its empty state would offer the wrong import.
        """
        shards_action = self.catalog_actions.get(SHARDS_VIEW_ID)
        ligands_action = self.catalog_actions.get(LIGANDS_VIEW_ID)
        sharded, ligand_rows = self._library_shape()
        if shards_action is not None:
            shards_action.setVisible(sharded)
            if not sharded:
                self.w.central_widget.close_view(SHARDS_VIEW_ID)
        if ligands_action is not None:
            ligands_action.setEnabled(bool(ligand_rows) or not sharded)
        self._sync_contextual_chrome()

    def _sync_contextual_chrome(self) -> None:
        """The contextual group's gap and separator go when none of its views is shown."""
        shown = any(
            action.isVisible()
            for _, view_id, _ in CONTEXTUAL_VIEWS
            if (action := self.catalog_actions.get(view_id)) is not None
        )
        for action in self.contextual_chrome:
            action.setVisible(shown)

    def _add_group_gap(self, bar) -> tuple[QAction, QAction, QAction]:
        """A wider separator: padding, the native `|`, padding."""
        def pad():
            widget = QWidget(bar)
            widget.setFixedWidth(self.GROUP_GAP_PX)
            return bar.addWidget(widget)

        return pad(), bar.addSeparator(), pad()

    def _library_shape(self) -> tuple[bool, int]:
        """`(library is sharded, how many ligand rows exist)` — two small indexed counts.

        ponytail: read on every badge sync (project open + end of each job) rather than cached;
        both flip exactly when a job finishes, which is when this runs.
        """
        if not self.w._has_active_project():
            return False, 0
        try:
            return self.w.runtime.molecules.library_shape()
        except Exception:  # noqa: BLE001 - a project that cannot be read shows the plain bar
            return False, 0

    def _add_data_action(self, bar, label: str, view_id: str, icon_name: str, before=None):
        action = QAction(load_icon(icon_name), label, bar)
        bar.addAction(action) if before is None else bar.insertAction(before, action)
        action.setCheckable(True)
        action.setChecked(self.w.central_widget.open_view(view_id) is not None)
        action.triggered.connect(lambda _=False, v=view_id: self.on_catalog_clicked(v))
        self.catalog_actions[view_id] = action
        return action

    def saved_toolbar_button_style(self) -> str:
        key = str(self.w._settings.value(self.TOOLBAR_STYLE_KEY, "icon_text") or "icon_text")
        return key if key in self.TOOLBAR_BUTTON_STYLES else "icon_text"

    def set_toolbar_button_style(self, key: str) -> None:
        from PySide6.QtCore import QSize

        _, style, icon_px, font_px, width, logo_px = self.TOOLBAR_BUTTON_STYLES.get(
            key, self.TOOLBAR_BUTTON_STYLES["icon_text"]
        )
        font_css = f"font-size: {font_px}px;" if font_px else ""
        self.catalog_toolbar.setToolButtonStyle(style)
        self.catalog_toolbar.setIconSize(QSize(icon_px, icon_px))
        if self.logo is not None:  # a bar widget, not an action: it does not follow the bar
            self.logo.setIconSize(QSize(logo_px, logo_px))
            self.logo.setFixedWidth(width - self.LOGO_INSET_PX)
        self.catalog_toolbar.setStyleSheet(f"QToolButton {{ {font_css} padding: 2px 4px; }}")
        if self.w.dock_manager is not None:
            try:
                self.w.dock_manager.set_button_style(
                    style, icon_px=icon_px, font_px=font_px, width=width
                )
            except AttributeError:  # older ms_components without the 3-mode API
                self.w.dock_manager.set_show_tool_names(style != Qt.ToolButtonIconOnly)
        self.w._settings.setValue(self.TOOLBAR_STYLE_KEY, key)

    def on_catalog_clicked(self, view_id: str) -> None:
        open_now = self.w.central_widget.open_view(view_id) is not None
        is_current = self.w.central_widget.current_view_id() == view_id
        if open_now and is_current:
            self.w.central_widget.close_view(view_id)
        else:
            self.open_or_focus_view(view_id)
        # Qt auto-toggled the check on click; re-sync it to the tab's real state.
        self.sync_catalog_action(view_id, self.w.central_widget.open_view(view_id) is not None)

    def sync_catalog_action(self, view_id: str, is_open: bool) -> None:
        action = self.catalog_actions.get(view_id)
        if action is None:
            return
        action.blockSignals(True)
        action.setChecked(bool(is_open))
        action.blockSignals(False)
        if view_id in EPHEMERAL_VIEW_IDS:
            action.setVisible(bool(is_open))
            self._sync_contextual_chrome()

    # -- workflow quick access -----------------------------------------------------

    def wire_workflow_quick_access(self) -> None:
        """Workflow is a standalone CHECKABLE left-sidebar action (not a dock): top of the bar.
        Checked shows the Workflow tab; unchecking hides it; closing the tab unchecks the action."""
        self.workflow_action_button = None
        if self.w.dock_manager is None:
            return
        # try:
        #     self.workflow_action_button = self.w.dock_manager.add_action_button(
        #         "workflow",
        #         region=Region.LEFT_TOP,
        #         order=-1,  # above every dock button (Catalog is order 1)
        #         title="Workflow",
        #         icon=load_icon("workflow.svg"),
        #         tooltip="Show/hide the active workflow pipeline.",
        #         checkable=True,
        #         on_click=self.on_workflow_toggled,
        #     )
        # except Exception:
        #     pass  # quick-access is optional chrome; never block window construction on it

    def on_workflow_toggled(self, checked: bool) -> None:
        # User clicked the sidebar action: open the tab when checked, close it when unchecked.
        if checked:
            self.open_or_focus_view(WORKFLOW_VIEW_ID)
        else:
            self.w.central_widget.close_view(WORKFLOW_VIEW_ID)

    def sync_workflow_action(self, view_id: str, is_open: bool) -> None:
        # Keep the sidebar action in lockstep with the tab (opened/closed by any route),
        # setting state without re-emitting toggled to avoid feedback loops.
        if view_id != WORKFLOW_VIEW_ID:
            return
        button = self.workflow_action_button
        if button is None:
            return
        button.blockSignals(True)
        button.setChecked(bool(is_open))
        button.blockSignals(False)
