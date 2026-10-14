"""The left tool panel: showing one tool at a time and keeping its button in sync."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QWidget

from amdockvs.ui.registry import TOOLS
from amdockvs.ui.resources.icons import icon as load_icon
from ms_components.ms_dockwidget.widget import Region


class ToolCoordinator:
    def __init__(self, window):
        self.w = window
        self.active_tool: str | None = None
        # Tools are built on first open and then kept: switching tools flips a page, so each
        # one is found on the step and with the settings it was left with. The stack is the
        # dock's only widget; a hidden page still gets hideEvent/showEvent, which is what the
        # tools use to release and re-take the catalog tables they borrow.
        self.stack = QStackedWidget()
        self.widgets: dict[str, QWidget] = {}
        self.action_buttons: dict[str, object] = {}

    @property
    def tool_widget(self) -> QWidget | None:
        return self.widgets.get(self.active_tool)

    def open_tool(self, view_id: str) -> QWidget:
        """Show a tool's config UI in the left tool panel (one tool at a time)."""
        window = self.w
        if getattr(window, "tools_dock", None) is None:
            return window.central_widget.open_or_focus_view(view_id)  # fallback: no dock host
        widget = self.widgets.get(view_id)
        if widget is None:
            title, widget = window.central_widget.build_view_widget(view_id)
            widget.setWindowTitle(title)
            self.widgets[view_id] = widget
            self.stack.addWidget(widget)
        previous_tool, was_open = self.active_tool, window.tools_dock.isVisible()
        if previous_tool != view_id:
            self.active_tool = view_id
            self.stack.setCurrentWidget(widget)  # hides the previous page, then shows this one
            window.tools_dock.setWindowTitle(widget.windowTitle())
            if previous_tool is not None:
                self.sync_tool_action(previous_tool, False)
            self.sync_tool_action(view_id, True)
            window.aux.set_occupant(view_id)
        window.dock_manager.toggle("tools", True)
        window.tools_dock.raise_()
        if not was_open:
            try:  # open at ~1/3 of the window, matching the right (PyMOL) dock
                window.resizeDocks([window.tools_dock], [window._third_width()], Qt.Horizontal)
            except Exception:
                pass
        return widget

    def on_tools_dock_visibility(self, visible: bool) -> None:
        # Hiding the tool panel (its close button) closes the active tool: unpress its toolbar
        # button and retire the auxiliary panel it contributed. The widget stays in the stack,
        # hidden with the dock, so reopening the tool resumes it.
        if visible or self.active_tool is None:
            return
        closed, self.active_tool = self.active_tool, None
        self.sync_tool_action(closed, False)
        self.w.aux.set_occupant(None)

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
