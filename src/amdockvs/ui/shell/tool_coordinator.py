"""The left tool panel: mounting one tool at a time and keeping its button in sync."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.tools.docking.studio import DOCKING_VIEW_ID
from amdockvs.ui.tools.molecules.build import BUILD_ID
from amdockvs.ui.tools.molecules.diversity import SELECTION_VIEW_ID
from amdockvs.ui.tools.molecules.filter import FILTER_ID
from amdockvs.ui.tools.molecules.pocket_detection import POCKET_DETECTION_VIEW_ID
from ms_components.ms_dockwidget.widget import Region


class ToolCoordinator:
    # -- Left toolbar: one flat checkable button per TOOL, no menus --
    # The left bar names tools only (things that mount a config UI in the tools dock);
    # anything that opens a central tab is data and lives in the top toolbar instead.
    # Each: (action_id, title, view_id, icon, order). Order 1 is the (hidden) tools dock
    # button, so tools start at 2; they share LEFT_BOTTOM, which puts an automatic
    # separator between them and the panel toggles (Workflow -1 / Details 0, LEFT_TOP).
    TOOL_ACTIONS = (
        ("tool_filter", "Filter", FILTER_ID, "filter.svg", 2),
        ("tool_diversity", "Diversity", SELECTION_VIEW_ID, "diversity.svg", 3),
        ("tool_build", "Build", BUILD_ID, "build.svg", 4),
        ("tool_pockets", "Pocket Detection", POCKET_DETECTION_VIEW_ID, "binding_site.svg", 5),
        ("tool_docking", "Docking Studio", DOCKING_VIEW_ID, "target.svg", 6),
    )

    # Config-time views: only meaningful while you are setting the tool up, so the top
    # toolbar shows them only while that tool is open. tool view_id -> entries.
    # Prep Status is no longer here: it is the child table of Ligands/Receptors, not a peer
    # tab of them, so it moved to the auxiliary zone (see AuxiliaryPanelController).
    TOOL_DATA_VIEWS: dict[str, tuple] = {}

    def __init__(self, window):
        self.w = window
        self.active_tool: str | None = None
        self.tool_widget: QWidget | None = None
        self.action_buttons: dict[str, object] = {}

    def open_tool(self, view_id: str) -> QWidget:
        """Mount a tool's config UI in the left tool panel (one tool at a time)."""
        window = self.w
        if getattr(window, "tools_dock", None) is None:
            return window.central_widget.open_or_focus_view(view_id)  # fallback: no dock host
        if self.active_tool == view_id and self.tool_widget is not None:
            window.dock_manager.toggle("tools", True)
            window.tools_dock.raise_()
            return self.tool_widget

        title, widget = window.central_widget.build_view_widget(view_id)
        old, previous_tool = window.tools_dock.widget(), self.active_tool
        window.tools_dock.setWidget(widget)  # reparents `old` out of the dock
        window.tools_dock.setWindowTitle(title)
        self.tool_widget, self.active_tool = widget, view_id
        if old is not None and old is not widget:
            old.deleteLater()
        if previous_tool is not None and previous_tool != view_id:
            self.sync_tool_action(previous_tool, False)
        self.sync_tool_action(view_id, True)
        window.views.set_contextual_data_views(view_id)
        window.aux.set_occupant(view_id)
        window.dock_manager.toggle("tools", True)
        window.tools_dock.raise_()
        try:  # open at ~1/3 of the window, matching the right (PyMOL) dock
            window.resizeDocks([window.tools_dock], [window._third_width()], Qt.Horizontal)
        except Exception:
            pass
        return widget

    def on_tools_dock_visibility(self, visible: bool) -> None:
        # Hiding the tool panel (its close button) closes the active tool: swap in an
        # empty placeholder, drop the widget, unpress its toolbar button and retire the
        # data views it contributed to the top toolbar.
        if visible or self.active_tool is None:
            return
        closed, self.active_tool = self.active_tool, None
        # Retire the tool's chrome BEFORE swapping the widget out: setWidget() fires the
        # tool's hideEvent, and a tool that throws in there must not strand the window
        # showing that tool's data views and auxiliary.
        self.sync_tool_action(closed, False)
        self.w.views.set_contextual_data_views(None)
        self.w.aux.set_occupant(None)
        old = self.w.tools_dock.widget()
        self.w.tools_dock.setWidget(QWidget())  # keep the dock valid without a tool
        self.tool_widget = None
        if old is not None:
            old.deleteLater()

    def build_tool_actions(self) -> None:
        self.action_buttons = {}  # view_id -> QToolButton
        if self.w.dock_manager is None:
            return
        for action_id, title, view_id, icon_name, order in self.TOOL_ACTIONS:
            self.action_buttons[view_id] = self.w.dock_manager.add_action_button(
                action_id,
                region=Region.LEFT_BOTTOM,
                order=order,
                title=title,
                icon=load_icon(icon_name),
                tooltip=f"Open the {title} tool.",
                checkable=True,
                on_click=lambda checked, v=view_id: self.on_tool_action(v, checked),
            )
        # The tools dock's own button is redundant now that every tool opens it: a button
        # that can only show an empty panel. Hide it (explicit hide survives the toolbar
        # rebuild) and keep the dock itself for the close/drag chrome.
        button = getattr(self.w.dock_manager, "buttons", {}).get("tools")
        if button is not None:
            button.setVisible(False)

    def on_tool_action(self, view_id: str, checked: bool) -> None:
        if checked:
            self.open_tool(view_id)
        elif self.active_tool == view_id:
            self.w.dock_manager.toggle("tools", False)  # -> on_tools_dock_visibility closes it

    def sync_tool_action(self, view_id: str, is_open: bool) -> None:
        button = self.action_buttons.get(view_id)
        if button is None:
            return
        button.blockSignals(True)
        button.setChecked(bool(is_open))
        button.blockSignals(False)
