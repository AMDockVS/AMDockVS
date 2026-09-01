"""Central tabs + the top data toolbar + the workflow quick-access button.

Owns "which view is open and which button shows it"; the window keeps thin delegators
(register_main_view / open_or_focus_view / open_view) because widgets call them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QSizePolicy, QWidget

from amdockvs.ui.catalog import (
    BINDING_SITES_VIEW_ID,
    COMPLEX_PAIRS_VIEW_ID,
    COMPLEXES_VIEW_ID,
    LIGANDS_VIEW_ID,
    MOLECULES_VIEW_ID,
    RECEPTOR_VIEW_ID,
)
from amdockvs.ui.catalog.domain_views import LIGAND_ACTIVITY_VIEW_ID
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.tools.docking.studio import DOCKING_VIEW_ID
from amdockvs.ui.tools.molecules.build import BUILD_ID
from amdockvs.ui.tools.molecules.diversity import SELECTION_VIEW_ID
from amdockvs.ui.tools.molecules.filter import FILTER_ID
from amdockvs.ui.tools.molecules.pocket_detection import POCKET_DETECTION_VIEW_ID
from amdockvs.ui.tools.qsar.panels import PREDICTIONS_VIEW_ID, QSAR_MODELS_VIEW_ID
from amdockvs.ui.tools.workflow_panel import WORKFLOW_VIEW_ID
from ms_components.ms_dockwidget.widget import Region


class ViewCoordinator:
    # Views opened to look at something and then abandoned. They share one tab slot (§0.5),
    # so they stop accumulating in the tab bar.
    PREVIEW_VIEW_IDS = frozenset({QSAR_MODELS_VIEW_ID, BINDING_SITES_VIEW_ID, COMPLEX_PAIRS_VIEW_ID})

    # Views that open in the left tool panel (beside the catalog tables) instead of a
    # central tab. Everything else — catalog + result tables — opens as a tab.
    TOOL_VIEW_IDS = frozenset({
        FILTER_ID, SELECTION_VIEW_ID, BUILD_ID, POCKET_DETECTION_VIEW_ID, DOCKING_VIEW_ID,
    })

    # Catalog reference tables live in a top toolbar of checkable actions (checked = tab
    # open), separated from the left tool buttons so "pick a table" doesn't collide with
    # "pick a tool". See build_catalog_toolbar.
    CATALOG_TABLES = (
        ("Molecules", MOLECULES_VIEW_ID, "catalog.svg"),
        ("Ligands", LIGANDS_VIEW_ID, "ligands.svg"),
        ("Receptors", RECEPTOR_VIEW_ID, "receptor.svg"),
        ("Binding Sites", BINDING_SITES_VIEW_ID, "binding_site.svg"),
        ("Complexes", COMPLEX_PAIRS_VIEW_ID, "complexes.svg"),
        ("Activity", LIGAND_ACTIVITY_VIEW_ID, "activity.svg"),
    )

    # Result views: once the data exists in the project it outlives the tool that produced
    # it, so these stay reachable with every tool closed. One group per separator.
    STANDING_DATA_VIEWS = (
        (
            # Off-target and Redocking are pivots inside this view, not entries of their own:
            # the same results read differently (see tools/docking/results_pivot.py).
            ("Docking Results", COMPLEXES_VIEW_ID, "docking_results.svg"),
        ),
        (
            ("QSAR Models", QSAR_MODELS_VIEW_ID, "models.svg"),
            ("Predictions", PREDICTIONS_VIEW_ID, "predictions.svg"),
        ),
    )

    # View > Toolbars. key -> (menu label, Qt style, icon px, font px, side-toolbar width).
    # Applies to every toolbar: the top catalog bar and the left/right dock toolbars.
    TOOLBAR_BUTTON_STYLES = {
        "icons": ("Icons Only", Qt.ToolButtonIconOnly, 24, None, 32),
        "icon_text": ("Icon + Text", Qt.ToolButtonTextUnderIcon, 20, 9, 64),
        "text": ("Text Only", Qt.ToolButtonTextOnly, 20, 12, 110),
    }
    TOOLBAR_STYLE_KEY = "ui/toolbar_button_style"

    def __init__(self, window):
        self.w = window
        self.catalog_actions: dict[str, QAction] = {}
        self.catalog_toolbar = None
        self.contextual_actions: list[tuple[str, object]] = []
        self.aux_anchor = None
        self.workflow_action_button = None

    # -- central tabs --------------------------------------------------------------

    def register_main_view(self, view_id: str, title: str, factory, *, on_close=None) -> None:
        self.w.central_widget.register_view(
            view_id, title, factory, on_close=on_close,
            preview=view_id in self.PREVIEW_VIEW_IDS,
        )

    def open_or_focus_view(self, view_id: str) -> QWidget:
        if view_id in self.TOOL_VIEW_IDS:
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

        Sections: catalog tables | standing result views | the active tool's config-time
        views, the last one repopulated by set_contextual_data_views on every tool change."""
        from PySide6.QtWidgets import QToolBar

        self.catalog_actions = {}  # view_id -> checkable QAction
        bar = QToolBar("Data", self.w)
        bar.setObjectName("catalog_toolbar")
        bar.setMovable(False)
        self.w.addToolBar(Qt.TopToolBarArea, bar)
        self.catalog_toolbar = bar
        for entries in (self.CATALOG_TABLES, *self.STANDING_DATA_VIEWS):
            for entry in entries:
                self._add_data_action(bar, *entry)
            bar.addSeparator()
        # Everything past that trailing separator belongs to the active tool.
        self.contextual_actions = []
        # The auxiliary-zone toggle is not a table: it floats right, past a stretch, so the
        # tool's contextual views (inserted before it) never push it around.
        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.aux_anchor = bar.addWidget(spacer)
        # Neutral label on purpose: the toggle owns the band, not what lands in it.
        # The action itself belongs to the auxiliary zone; the bar only hosts it.
        self.w.aux.install_action(bar.addAction(load_icon("details.svg"), "Panel"))
        # Every toolbar button now exists (docks + actions + these), so one pass styles them all.
        self.set_toolbar_button_style(self.saved_toolbar_button_style())

    def _add_data_action(self, bar, label: str, view_id: str, icon_name: str, before=None):
        action = QAction(load_icon(icon_name), label, bar)
        bar.addAction(action) if before is None else bar.insertAction(before, action)
        action.setCheckable(True)
        action.setChecked(self.w.central_widget.open_view(view_id) is not None)
        action.triggered.connect(lambda _=False, v=view_id: self.on_catalog_clicked(v))
        self.catalog_actions[view_id] = action
        return action

    def set_contextual_data_views(self, tool_view_id: str | None) -> None:
        """Swap the tail section of the top toolbar to the active tool's data views."""
        bar = self.catalog_toolbar
        if bar is None:
            return
        for view_id, action in self.contextual_actions:
            bar.removeAction(action)
            self.catalog_actions.pop(view_id, None)
        # No restyle pass needed: QToolBar applies its own style/iconSize to new actions.
        self.contextual_actions = [
            (view_id, self._add_data_action(bar, label, view_id, icon_name, self.aux_anchor))
            for label, view_id, icon_name in self.w.tools.TOOL_DATA_VIEWS.get(tool_view_id, ())
        ]

    def saved_toolbar_button_style(self) -> str:
        key = str(self.w._settings.value(self.TOOLBAR_STYLE_KEY, "icon_text") or "icon_text")
        return key if key in self.TOOLBAR_BUTTON_STYLES else "icon_text"

    def set_toolbar_button_style(self, key: str) -> None:
        from PySide6.QtCore import QSize

        _, style, icon_px, font_px, width = self.TOOLBAR_BUTTON_STYLES.get(
            key, self.TOOLBAR_BUTTON_STYLES["icon_text"]
        )
        font_css = f"font-size: {font_px}px;" if font_px else ""
        self.catalog_toolbar.setToolButtonStyle(style)
        self.catalog_toolbar.setIconSize(QSize(icon_px, icon_px))
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

    # -- workflow quick access -----------------------------------------------------

    def wire_workflow_quick_access(self) -> None:
        """Workflow is a standalone CHECKABLE left-sidebar action (not a dock): top of the bar.
        Checked shows the Workflow tab; unchecking hides it; closing the tab unchecks the action."""
        self.workflow_action_button = None
        if self.w.dock_manager is None:
            return
        try:
            self.workflow_action_button = self.w.dock_manager.add_action_button(
                "workflow",
                region=Region.LEFT_TOP,
                order=-1,  # above every dock button (Catalog is order 1)
                title="Workflow",
                icon=load_icon("workflow.svg"),
                tooltip="Show/hide the active workflow pipeline.",
                checkable=True,
                on_click=self.on_workflow_toggled,
            )
        except Exception:
            pass  # quick-access is optional chrome; never block window construction on it

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
