"""The auxiliary zone below the central table.

It holds ONE occupant at a time and a single global toggle shows or hides it — the toggle
never picks the contents. Details is the standing occupant (it follows the selection of any
table); an active tool borrows the slot and gives it back on close, the same push/pop the
tools will use for the center tables.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from amdockvs.ui.catalog.details import CatalogDetailsView
from amdockvs.ui.tools.docking.studio import DOCKING_VIEW_ID, PREP_STATUS_VIEW_ID
from amdockvs.ui.tools.molecules.pocket_detection import POCKET_DETECTION_VIEW_ID, POCKET_SITES_VIEW_ID


class AuxiliaryPanelController:
    AUX_DETAILS = "aux.details"
    # A central view can claim the slot too, by exposing `aux_panel()` (Docking Results hands
    # over its "Selected Result"). It outranks the tool's auxiliary: the panel belongs to the
    # table you are looking at, and it goes back when you leave the tab.
    AUX_VIEW_PANEL = "aux.view_panel"
    TOOL_AUX_VIEWS = {
        DOCKING_VIEW_ID: PREP_STATUS_VIEW_ID,
        POCKET_DETECTION_VIEW_ID: POCKET_SITES_VIEW_ID,
    }

    def __init__(self, window):
        self.w = window
        self.views: dict[str, QWidget] = {}  # auxiliary-zone pages, built on first use
        self.occupant = self.AUX_DETAILS
        self.view_panel: QWidget | None = None  # panel lent by the tab on screen
        self.action = None  # the toolbar toggle, installed by ViewCoordinator
        self._seeded = False
        self._user_checked = False  # panel state to give back when a tool releases the slot
        self.last_selection: tuple[str, object] | None = None

    def install_action(self, action) -> None:
        self.action = action
        action.setCheckable(True)
        action.setToolTip("Show/hide the panel below the table (details of the selection).")
        action.toggled.connect(self.on_toggled)

    def page_for(self, view_id: str) -> QWidget | None:
        """The auxiliary page a tool owns, built on first ask. It may be off screen: the page
        keeps what it was told and defers the loading itself (A0), so the tool can push
        without checking whether the panel happens to be open."""
        if view_id not in self.TOOL_AUX_VIEWS.values():
            return None
        return self._page(view_id)

    def _page(self, aux_id: str) -> QWidget:
        """Build an occupant once, on first use, and keep it in the stack."""
        page = self.views.get(aux_id)
        if page is None:
            if aux_id == self.AUX_VIEW_PANEL:
                page = self.view_panel  # owned by the view, only borrowed by the stack
                self.views[aux_id] = page
                self.w.central_widget.aux_widget.addWidget(page)
                return page
            if aux_id == self.AUX_DETAILS:
                page = self._build_details_view()
            else:
                page = self.w.central_widget.build_view_widget(aux_id)[1]
            self.views[aux_id] = page
            self.w.central_widget.aux_widget.addWidget(page)
        return page

    def _current_view_aux_panel(self) -> QWidget | None:
        """The panel the tab on screen wants in the slot, if it offers one."""
        view_id = self.w.central_widget.current_view_id()
        view = self.w.central_widget.open_view(view_id) if view_id else None
        panel = getattr(view, "aux_panel", None)
        return panel() if callable(panel) else None

    def set_occupant(self, *_args) -> None:
        """Hand the slot to the view on screen, else the active tool's auxiliary, else Details.
        Called on every tool AND tab change: `*_args` swallows the signals' payloads."""
        panel = self._current_view_aux_panel()
        occupant = (
            self.AUX_VIEW_PANEL if panel is not None
            else self.TOOL_AUX_VIEWS.get(self.w.tools.active_tool, self.AUX_DETAILS)
        )
        if occupant == self.occupant and panel is self.view_panel:
            return
        if self.view_panel is not None and panel is not self.view_panel:
            # Give the old panel back before it outlives its view: the stack is only a host.
            stale = self.views.pop(self.AUX_VIEW_PANEL, None)
            if stale is not None:
                self.w.central_widget.aux_widget.removeWidget(stale)
                stale.setParent(None)
        self.view_panel = panel
        self.occupant = occupant
        if occupant != self.AUX_DETAILS:
            # A tool's own output is not something to go hunting for behind a toggle:
            # claiming the slot opens the panel, and closing gives back the state found.
            self._user_checked = self.action.isChecked()
            self.action.setChecked(True)
        elif not self._user_checked:
            self.action.setChecked(False)
        if self.action.isChecked():
            self.on_toggled(True)

    def reset(self) -> None:
        """Drop the auxiliary contents so they rebuild against the new project context."""
        for aux_id, page in self.views.items():
            self.w.central_widget.aux_widget.removeWidget(page)
            page.setParent(None) if aux_id == self.AUX_VIEW_PANEL else page.deleteLater()
        self.views = {}
        self.view_panel, self.occupant = None, self.AUX_DETAILS
        if self.action.isChecked():
            self.on_toggled(True)  # rebuild in place; the panel stays open

    def on_toggled(self, checked: bool) -> None:
        aux = self.w.central_widget.aux_widget
        if not checked:
            aux.setVisible(False)
            return
        page = self._page(self.occupant)
        aux.setCurrentWidget(page)
        aux.setVisible(True)
        if self.occupant == self.AUX_DETAILS:
            # It stopped tracking the selection while hidden; catch up now that it shows.
            self._push_selection_to_details(page)
        if not self._seeded:
            # An occupant's size hint is tall enough to swallow the table on the first show;
            # seed a 70/30 split once. After that the splitter keeps whatever you drag.
            self._seeded = True
            splitter = self.w.central_widget.central_widget
            height = splitter.height()
            splitter.setSizes([height * 7 // 10, height * 3 // 10])

    def refresh(self) -> None:
        """Same rule as the tabs: only the occupant on screen reloads."""
        page = self.views.get(self.occupant)
        if page is None or not page.isVisible():
            return
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    # -- Details page --------------------------------------------------------------

    def _build_details_view(self) -> QWidget:
        viewer = self.w.viewer
        view = CatalogDetailsView(runtime=self.w.runtime, parent=self.w.central_widget)
        view.show_molecule_requested.connect(viewer.show_molecule)
        view.show_binding_site_requested.connect(viewer.show_binding_site)
        view.show_complex_requested.connect(viewer.show_complex)
        view.show_file_requested.connect(viewer.show_file)
        self._push_selection_to_details(view)  # open non-blank on the current selection
        return view

    def _push_selection_to_details(self, view) -> None:
        selection = self.last_selection
        if not selection or selection[1] is None:
            view.clear_details()
        elif str(selection[0]).strip().lower() == "complex":
            view.show_complex(selection[1])
        else:
            view.show_molecule(selection[1])

    def show_catalog_selection_details(self, kind: str, obj) -> None:
        # Details sits in the auxiliary zone and tracks the selection only while visible —
        # show_molecule() costs 4 queries on the GUI thread (see §3), too much to pay for a
        # hidden panel; on_toggled replays last_selection when it comes back. The grid
        # preview runs regardless.
        window = self.w
        self.last_selection = (str(kind or ""), obj)
        details = self.views.get(self.AUX_DETAILS)
        if details is not None and not details.isVisible():
            details = None  # zone hidden, or a tool is borrowing the slot
        if obj is None:
            if details is not None:
                details.clear_details()
            if window.grid_dock is not None:
                window.grid_dock.clear_molecule()
            window.viewer.hide_grid_panel()
            return
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind == "complex":
            if details is not None:
                details.show_complex(obj)
            try:
                receptor = window.runtime.molecules.get(
                    int(getattr(obj, "receptor_molecule_id", 0) or 0)
                )
                if receptor is not None and bool(getattr(receptor, "is_receptor", False)):
                    if window.grid_dock is not None:
                        window.grid_dock.focus_binding_site(
                            receptor,
                            site_id=int(getattr(obj, "binding_site_id", 0) or 0),
                            ensure_selected=True,
                        )
                else:
                    if window.grid_dock is not None:
                        window.grid_dock.clear_molecule()
                if window.viewer.grid_preview_enabled and receptor is not None and bool(
                        getattr(receptor, "is_receptor", False)):
                    window.viewer.show_grid_panel()
                else:
                    window.viewer.hide_grid_panel()
            except Exception:
                if window.grid_dock is not None:
                    window.grid_dock.clear_molecule()
                window.viewer.hide_grid_panel()
            return
        if details is not None:
            details.show_molecule(obj)
        if window.grid_dock is not None:
            window.grid_dock.set_molecule(obj if bool(getattr(obj, "is_receptor", False)) else None)
        if window.viewer.grid_preview_enabled and bool(getattr(obj, "is_receptor", False)):
            window.viewer.show_grid_panel()
        else:
            window.viewer.hide_grid_panel()
