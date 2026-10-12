"""Small shared widget helpers. Nothing here is a component — just Qt spellings we repeat."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QMenu, QSizePolicy, QToolButton, QWidget

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR

# ponytail: off until a workflow can run steps that wait on other jobs; flip it on then.
WORKFLOW_SAVE_READY = False


def gate_workflow_save(control) -> None:
    """Disable a 'Save to workflow' button or menu action until workflows can chain jobs."""
    if not WORKFLOW_SAVE_READY:
        control.setEnabled(False)
        control.setToolTip("Not available yet: a workflow cannot run steps that depend on other jobs.")


def split_button(text: str, parent: QWidget, *, on_click, primary: bool = False) -> QToolButton:
    """Push button with a menu arrow welded to its right: the body runs the primary action, the
    arrow drops the alternatives. Qt does this natively (MenuButtonPopup) — fill the menu with
    `button.menu().addAction(...)`. `primary=True` makes it the big bold CTA of its step."""
    button = QToolButton(parent)
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonTextOnly)
    button.setPopupMode(QToolButton.MenuButtonPopup)
    # QToolButton's default vertical policy grows; a QPushButton's doesn't. Without this the
    # button stretches to the full height of whatever layout holds it.
    button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    if primary:
        button.setMinimumSize(180, 44)
        font = button.font()
        font.setPointSizeF(font.pointSizeF() + 2.0)
        font.setBold(True)
        button.setFont(font)
    button.clicked.connect(on_click)
    menu = QMenu(button)
    menu.setToolTipsVisible(True)  # off by default in QMenu, and these entries need the why
    button.setMenu(menu)
    return button


def right_aligned(widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(widget)
    return row


class RunDestinationCombo(QComboBox):
    """Where to send this job: one of the executors that are active, the default one first.

    They are started and stopped in the Executors panel, so the list is read again each time
    this is shown or opened; what was chosen stays chosen while it is still offered.
    """

    def __init__(self, parent: QWidget, runtime) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self.setToolTip("Who runs this job, among the executors that are active. They are started in Executors.")
        self.reload(runtime)

    def reload(self, runtime=None) -> None:
        current = self.currentData()
        try:
            destinations = (runtime or self._runtime).run_destinations()
        except Exception:  # noqa: BLE001 - no project open yet
            destinations = []
        if destinations == [(self.itemData(i), self.itemText(i)) for i in range(self.count())]:
            return
        self.clear()
        for name, label in destinations:
            self.addItem(label, name)
        self.setCurrentIndex(max(0, self.findData(current)))

    def showEvent(self, event) -> None:
        self.reload()
        super().showEvent(event)

    def showPopup(self) -> None:
        self.reload()
        super().showPopup()

    def executor_name(self) -> str:
        return str(self.currentData() or DEFAULT_LOCAL_CPU_EXECUTOR)


__all__ = ["RunDestinationCombo", "right_aligned", "split_button"]
