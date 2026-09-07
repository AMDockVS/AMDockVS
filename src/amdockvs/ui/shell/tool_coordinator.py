"""The left tool panel: mounting one tool at a time and keeping its button in sync."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from amdockvs.ui.registry import TOOLS
from amdockvs.ui.resources.icons import icon as load_icon
from ms_components.ms_dockwidget.widget import Region


class ToolCoordinator:
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
        # auxiliary panel it contributed.
        if visible or self.active_tool is None:
            return
        closed, self.active_tool = self.active_tool, None
        # Retire the tool's chrome BEFORE swapping the widget out: setWidget() fires the
        # tool's hideEvent, and a tool that throws in there must not strand the window
        # showing that tool's data views and auxiliary.
        self.sync_tool_action(closed, False)
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
        for tool in TOOLS:
            self.action_buttons[tool.view_id] = self.w.dock_manager.add_action_button(
                tool.action_id,
                region=Region.LEFT_BOTTOM,
                order=tool.order,
                title=tool.title,
                icon=load_icon(tool.icon),
                tooltip=f"Open the {tool.title} tool.",
                checkable=True,
                on_click=lambda checked, v=tool.view_id: self.on_tool_action(v, checked),
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
