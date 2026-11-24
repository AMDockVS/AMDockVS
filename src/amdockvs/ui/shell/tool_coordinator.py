"""The left tool panel: showing one tool at a time and keeping its button in sync."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QStackedWidget, QStyle, QWidget

from amdockvs.ui.registry import TOOLS
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, LIGANDS_VIEW_ID, MOLECULES_VIEW_ID, RECEPTOR_VIEW_ID
from ms_components.ms_dockwidget.widget import Region


class _CurrentPageStack(QStackedWidget):
    """A QStackedWidget sizes to its largest page: Diversity's 728 px kept Build from shrinking.

    Only the page on show counts, so each tool can be narrowed to its own minimum.
    """

    def __init__(self) -> None:
        super().__init__()
        self.currentChanged.connect(self.updateGeometry)

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()


# The central views a tool brings up on show or on a step (Filter/Build: Molecules, Diversity:
# Ligands, Pockets: Receptors, Docking Studio's run step: Docking Results).
STEP_VIEWS = (MOLECULES_VIEW_ID, LIGANDS_VIEW_ID, RECEPTOR_VIEW_ID, COMPLEXES_VIEW_ID)


class ToolCoordinator:
    SECTION_REGIONS = {"top": Region.LEFT_TOP, "middle": Region.LEFT_BOTTOM, "bottom": Region.BOTTOM_LEFT}

    def __init__(self, window):
        self.w = window
        self.active_tool: str | None = None
        # Tools are built on first open and then kept: switching tools flips a page, so each
        # one is found on the step and with the settings it was left with. The stack is the
        # dock's only widget; a hidden page still gets hideEvent/showEvent, which is what the
        # tools use to release and re-take the catalog tables they borrow.
        self.stack = _CurrentPageStack()
        self.widgets: dict[str, QWidget] = {}
        self.action_buttons: dict[str, object] = {}

    @property
    def tool_widget(self) -> QWidget | None:
        return self.widgets.get(self.active_tool)

    def _build(self, view_id: str) -> QWidget:
        title, widget = self.w.central_widget.build_view_widget(view_id)
        widget.setWindowTitle(title)
        self.widgets[view_id] = widget
        self.stack.addWidget(widget)
        return widget

    def warm_up(self) -> None:
        """Build every tool (with its auxiliary page, and the central views the tools bring up)
        hidden, and paint each once off screen.

        The first open of a tool cost up to ~2 s on the GUI thread (Docking Studio: its tables,
        the stepper, then Qt's first polish/layout/paint). Paid here, under the splash, the
        click only flips a page. grab() lays out and paints without a showEvent, so no tool
        opens its central tabs as a side effect.
        """
        window = self.w
        if getattr(window, "tools_dock", None) is None or not window._has_active_project():
            return  # tools are bound to the open project; built when one opens
        size = QSize(window._third_width(), max(window.height(), 600))
        for tool in TOOLS:
            widget = self.widgets.get(tool.view_id) or self._build(tool.view_id)
            pages = [widget]
            aux_id = window.aux.TOOL_AUX_VIEWS.get(tool.view_id)
            if aux_id is not None:
                pages.append(window.aux.page_for(aux_id))
            for page in pages:
                if page is not None and not page.isVisible():
                    page.resize(size)
                    page.grab()
        for view_id in STEP_VIEWS:
            window.central_widget.prebuild(view_id)

    def open_tool(self, view_id: str) -> QWidget:
        """Show a tool's config UI in the left tool panel (one tool at a time)."""
        window = self.w
        if getattr(window, "tools_dock", None) is None:
            return window.central_widget.open_or_focus_view(view_id)  # fallback: no dock host
        widget = self.widgets.get(view_id) or self._build(view_id)
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
            try:  # open at ~1/3 of the window, matching the right (PyMOL) dock...
                width = window._third_width()
                if getattr(widget, "dock_opens_at_size_hint", False):
                    # ...or at the tool's own width, plus room for its vertical scrollbar so a
                    # tall step doesn't scroll sideways; at most half the window.
                    scrollbar = widget.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
                    width = min(window.width() // 2, widget.sizeHint().width() + scrollbar)
                docks, widths = window.take_back_viewer_room() if hasattr(window, "take_back_viewer_room") else ([], [])
                window.resizeDocks([window.tools_dock, *docks], [width, *widths], Qt.Horizontal)
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
                region=self.SECTION_REGIONS[tool.section],
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
