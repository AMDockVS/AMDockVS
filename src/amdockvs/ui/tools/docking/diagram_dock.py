"""2D protein-ligand interaction diagram for the pose selected in Results.

Lives under PyMOL (Region.BOTTOM_RIGHT, bottom-in-lateral): the 3D pose and its 2D contact map are the two
readings of the same thing, so they share the right column. The diagram is a live Qt scene
(ms_contactmap's `InteractionDiagramWidget`) -- zoomable, draggable residues, rotate/mirror/
recompute in its toolbar -- not a rendered image.

The dock follows the ligand>pose selection and only shows a saved diagram document. It never
builds one: interactions are computed when asked for (the interactions job, the docking chain,
"Build diagram" in Selected Result or the dock's own Build diagram call to action), never
because a pose was clicked.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from amdockvs.project.summaries import DockingHitSummary
from ms_components.ms_dockwidget.widget import DockManager, MSDockWidget

_PLACEHOLDER = "Select a docking pose to see its 2D interaction diagram."


class InteractionDiagramDock(MSDockWidget):
    """Shows the saved diagram of the selected pose; building one is Build diagram's job."""

    #: Settings-tree node to open: embedded here, the diagram has no settings of its own.
    settings_requested = Signal(str)
    #: (hit, residue key, diagram) -- a residue picked in the diagram, for PyMOL to show.
    residue_selected = Signal(object, str, object)
    #: The selected pose has no diagram and the user pressed Build diagram here.
    build_requested = Signal()

    def __init__(self, title: str, dock_manager: DockManager, *, runtime, parent: QWidget | None = None):
        super().__init__(title, dock_manager, parent)
        self.runtime = runtime
        self._hit: DockingHitSummary | None = None
        self._pose_rank = 1
        self._diagram = None
        self.view: QWidget | None = None  # ms_contactmap widget, created on first diagram

        container = QWidget(self)
        body = QVBoxLayout(container)
        body.setContentsMargins(4, 4, 4, 4)
        self.stack = QStackedWidget(container)
        self.placeholder = QLabel(_PLACEHOLDER, self.stack)
        self.placeholder.setWordWrap(True)
        self.stack.addWidget(self.placeholder)
        body.addWidget(self.stack, 1)

        row = QHBoxLayout()
        self.status = QLabel("", container)
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        body.addLayout(row)
        self.setWidget(container)

    # --- selection ----------------------------------------------------------
    def show_hit(self, hit: DockingHitSummary | None, pose_rank: int = 1) -> None:
        self._hit = hit
        self._pose_rank = int(pose_rank or 1)
        ready = hit is not None and bool(hit.pose_payload_id and hit.receptor_payload_id)
        if not ready:
            self.placeholder.setText(_PLACEHOLDER)
            self.stack.setCurrentWidget(self.placeholder)
            self.status.setText("")
            return
        # A saved diagram is a JSON read plus a scene build (milliseconds), so the dock can
        # follow every click.
        from amdockvs.docking.results.diagram import load_pose_diagram

        cached = load_pose_diagram(str(hit.output_path), self._pose_rank)
        label = f"{hit.ligand_name} · pose {self._pose_rank}"
        if cached is None:
            self._diagram = None
            self._ensure_view().show_build_prompt()
            self.stack.setCurrentWidget(self.view)
            self.status.setText(f"{label} — no diagram yet")
            return
        self._draw(*cached, label=label)

    def reload(self) -> None:
        """Re-check the cache for the current selection (a diagram job may have just filled it)."""
        if self._hit is not None:
            self.show_hit(self._hit, self._pose_rank)

    # --- drawing ------------------------------------------------------------
    def _draw(self, diagram, layout, *, label: str) -> None:
        self._diagram = diagram
        self._ensure_view().set_layout(diagram, layout)
        self.stack.setCurrentWidget(self.view)
        self.status.setText(f"{label} — {len(diagram.interactions)} interactions")

    def _ensure_view(self) -> QWidget:
        if self.view is None:
            from ms_contactmap import InteractionDiagramWidget

            self.view = InteractionDiagramWidget(parent=self)
            self.view.set_configure_action(
                "Defaults…", lambda: self.settings_requested.emit("ms_contactmap")
            )
            self.view.residueSelected.connect(self._on_residue_selected)
            self.view.buildRequested.connect(self.build_requested)
            self.apply_settings()
            self.stack.addWidget(self.view)
        return self.view

    def _on_residue_selected(self, key: str) -> None:
        if key and self._hit is not None and self._diagram is not None:
            self.residue_selected.emit(self._hit, key, self._diagram)

    def apply_settings(self) -> None:
        """Restyle the diagram from Settings › MS-ContactMap (again whenever settings are saved)."""
        if self.view is not None:
            self.view.apply_view_settings(self.runtime.contact_map_configuration.get_value("").model_dump())
