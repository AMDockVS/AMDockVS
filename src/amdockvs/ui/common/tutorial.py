"""AMDockVS tutorial presentation: a clear background with the target outlined."""
from PySide6.QtCore import QEvent, QRectF, QTimer
from PySide6.QtGui import QPainter, QPalette, QPen
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid

from ms_components.ms_tutorial import TutorialController as BaseTutorialController
from ms_components.ms_tutorial.overlay import TutorialOverlay as BaseTutorialOverlay


class TutorialOverlay(BaseTutorialOverlay):
    def paintEvent(self, event):
        # The inherited mask still gates interaction around the target. Only the
        # paint changes: leave the rest of the application at its normal colors.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 3))
        painter.drawRoundedRect(QRectF(self.hole.adjusted(-3, -3, 3, 3)), 6, 6)


class TutorialController(BaseTutorialController):
    def __init__(self, *, instruction_provider=None, **kwargs):
        self.instruction_provider = instruction_provider
        super().__init__(**kwargs)

    def eventFilter(self, watched, event):
        # QDialog.accept() sends Close before finished/accepted. Saving a project
        # completes that dialog; it must not cancel the surrounding tutorial.
        if (self.is_active and self.overlay is not None
                and watched is self.overlay.parentWidget()
                and event.type() == QEvent.Type.Close
                and isinstance(watched, QDialog) and watched.result() == QDialog.Accepted):
            self.overlay.hide()
            QTimer.singleShot(0, self._refresh)
            return False
        return super().eventFilter(watched, event)

    def _refresh(self):
        # ms_tutorial currently creates its overlay directly. Supply our visual
        # variant first, then let its controller handle placement and navigation.
        step = self.current_step
        if step is not None:
            try:
                source = self.targets[step.target].widget
                target = source() if callable(source) else source
            except Exception:
                return super()._refresh()  # the shared controller reports binding errors
            if isinstance(target, QWidget) and isValid(target) and target.isVisible():
                host = target.window()
                if self.overlay is None or self.overlay.parentWidget() is not host:
                    if self.overlay is not None:
                        self.overlay.hide()
                        self.overlay.deleteLater()
                    self._overlay = TutorialOverlay(host)
                    self._overlay.next_requested.connect(self.next)
                    self._overlay.back_requested.connect(self.back)
                    self._overlay.exit_requested.connect(self.stop)
                    self.destroyed.connect(self._overlay.deleteLater)
        super()._refresh()
        if self.is_active and self.overlay is not None and self.instruction_provider is not None:
            text = self.instruction_provider(self.current_step)
            if self.overlay.body.toPlainText() != text:
                self.overlay.body.setPlainText(text)
                self.overlay._size_key = None  # remeasure the new instruction on the next placement
