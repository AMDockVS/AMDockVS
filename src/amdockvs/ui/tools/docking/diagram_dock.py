"""2D protein-ligand interaction diagram for the pose selected in Results.

Lives under PyMOL (Region.RIGHT_BOTTOM): the 3D pose and its 2D contact map are the two
readings of the same thing, so they share the right column. The diagram is a live Qt scene
(ms_contactmap's `InteractionDiagramWidget`) -- zoomable, draggable residues, rotate/mirror/
recompute in its toolbar -- not a rendered image.

The dock only *draws*: it follows the ligand>pose selection and shows whatever the saved
diagram document holds. Building is Selected Result's "Build diagram" (same pass that fills
its interaction list), so this view stays agnostic to poses, receptors and detection -- exactly
what ms_contactmap needs to be reusable outside AMDock.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from amdockvs.summaries import DockingHitSummary
from ms_components.ms_dockwidget.widget import DockManager, MSDockWidget

_PLACEHOLDER = "Select a docking pose to see its 2D interaction diagram."
_NOT_BUILT = "No diagram for this pose yet — build it from Selected Result › Interactions."


class InteractionDiagramDock(MSDockWidget):
    """Shows the selected pose's saved diagram; the panel that owns the pose builds it."""

    def __init__(self, title: str, dock_manager: DockManager, *, runtime, parent: QWidget | None = None):
        super().__init__(title, dock_manager, parent)
        self.runtime = runtime
        self._hit: DockingHitSummary | None = None
        self._pose_rank = 1
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
        ready = (
            hit is not None
            and hit.output_path is not None
            and hit.output_path.exists()
            and hit.receptor_path is not None
        )
        if not ready:
            self.placeholder.setText(_PLACEHOLDER)
            self.stack.setCurrentWidget(self.placeholder)
            self.status.setText("")
            return
        # A saved diagram is a JSON read plus a scene build (milliseconds), so the dock can
        # follow every click. Nothing saved: show the placeholder rather than the previous
        # pose's diagram, which would silently read as this one's.
        from amdockvs.docking.diagram import load_pose_diagram

        cached = load_pose_diagram(str(hit.output_path), self._pose_rank)
        label = f"{hit.ligand_name} · pose {self._pose_rank}"
        if cached is None:
            self.placeholder.setText(_NOT_BUILT)
            self.stack.setCurrentWidget(self.placeholder)
            self.status.setText(label)
            return
        self._draw(*cached, label=label)

    def reload(self) -> None:
        """Re-check the cache for the current selection (a diagram job may have just filled it)."""
        if self._hit is not None:
            self.show_hit(self._hit, self._pose_rank)

    # --- drawing ------------------------------------------------------------
    def _draw(self, diagram, layout, *, label: str) -> None:
        self._ensure_view().set_layout(diagram, layout)
        self.stack.setCurrentWidget(self.view)
        self.status.setText(f"{label} — {len(diagram.interactions)} interactions")

    def _ensure_view(self) -> QWidget:
        if self.view is None:
            from ms_contactmap import InteractionDiagramWidget

            self.view = InteractionDiagramWidget(parent=self)
            self.stack.addWidget(self.view)
        return self.view
