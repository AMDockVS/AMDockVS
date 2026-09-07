"""Central tabs + the top data toolbar + the workflow quick-access button.

Owns "which view is open and which button shows it"; the window keeps thin delegators
(register_main_view / open_or_focus_view / open_view) because widgets call them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from amdockvs.ui.catalog import LIGANDS_VIEW_ID, SHARDS_VIEW_ID
from amdockvs.ui.registry import (
    CATALOG_TABLES,
    PREVIEW_VIEW_IDS,
    STANDING_DATA_VIEWS,
    TOOL_VIEW_IDS,
)
from amdockvs.ui.resources.icons import icon as load_icon
from amdockvs.ui.tools.workflow_panel import WORKFLOW_VIEW_ID
from amdockvs.core.vocab import PROJECT_MODE_BADGES, PROJECT_MODE_LABELS, PROJECT_MODES, ProjectMode
from ms_components.ms_dockwidget.widget import Region


class ViewCoordinator:
    # Palette roles, not hex: theming lives in ms_components. The colour is the only thing
    # that changes with the mode — `vs` reads as plain text, `htpvs` gets the accent.
    MODE_BADGE_CSS = (
        "QLabel#mode_badge {{ margin: 0 6px; padding: 2px 8px; font-weight: 600;"
        " border: 1px solid palette(mid); border-radius: 4px; color: {colour}; }}"
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
        self.workflow_action_button = None
        self.mode_badge = None

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

        Sections: catalog tables | standing result views | auxiliary-panel toggle."""
        from PySide6.QtWidgets import QToolBar

        self.catalog_actions = {}  # view_id -> checkable QAction
        bar = QToolBar("Data", self.w)
        bar.setObjectName("catalog_toolbar")
        bar.setMovable(False)
        self.w.addToolBar(Qt.TopToolBarArea, bar)
        self.catalog_toolbar = bar
        # Which storage contract the project runs under, first thing on the bar: in `htpvs`
        # the catalog tables next to it do not hold the library, so the two belong together.
        self.mode_badge = QLabel(bar)
        self.mode_badge.setObjectName("mode_badge")
        bar.addWidget(self.mode_badge)
        bar.addSeparator()
        for entries in (CATALOG_TABLES, *STANDING_DATA_VIEWS):
            for entry in entries:
                self._add_data_action(bar, *entry)
            bar.addSeparator()
        # The auxiliary-zone toggle is not a table: it floats right, past a stretch, so the
        # catalog actions never push it around.
        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        # Neutral label on purpose: the toggle owns the band, not what lands in it.
        # The action itself belongs to the auxiliary zone; the bar only hosts it.
        self.w.aux.install_action(bar.addAction(load_icon("details.svg"), "Panel"))
        # After the actions exist, not before: the badge and the bar say the same thing.
        self.sync_mode_badge()
        # Every toolbar button now exists (docks + actions + these), so one pass styles them all.
        self.set_toolbar_button_style(self.saved_toolbar_button_style())

    def sync_mode_badge(self) -> None:
        """Show where this project's screening library lives; nothing to show with no project.

        Derived, not declared: it flips to HTP-VS when the project holds shards, which is why
        it is refreshed after every finished job and not only on open.
        """
        if self.mode_badge is None:
            return
        mode = getattr(self.w.runtime, "mode", "") if self.w._has_active_project() else ""
        index = PROJECT_MODES.index(mode) if mode in PROJECT_MODES else -1
        self.mode_badge.setVisible(index >= 0)
        self.sync_catalog_availability()
        if index < 0:
            return
        accent = "palette(highlight)" if mode == ProjectMode.HTPVS else "palette(text)"
        self.mode_badge.setStyleSheet(self.MODE_BADGE_CSS.format(colour=accent))
        self.mode_badge.setText(PROJECT_MODE_BADGES[index])
        self.mode_badge.setToolTip(
            f"Screening library: {PROJECT_MODE_LABELS[index]} — set by how the ligands were imported"
        )

    def sync_catalog_availability(self) -> None:
        """Which catalog tables this project has anything to say with.

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
