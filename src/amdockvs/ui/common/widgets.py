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
    """Where to send this job. Hides itself when there is only one place to send it.

    An HPC executor takes no local CPU token, so local work and cluster work run at the same
    time — this is a destination picker, not a backend switch. Which of loky/ray sits behind
    "Local" is the monitor's business, not this widget's.
    """

    def __init__(self, parent: QWidget, runtime) -> None:
        super().__init__(parent)
        self.setToolTip(
            "Where this job runs. Local uses this machine's cores; an HPC entry submits to "
            "that cluster's scheduler and costs no local CPU, so both can run at once."
        )
        self.reload(runtime)

    def reload(self, runtime) -> None:
        current = self.executor_name()
        self.clear()
        try:
            destinations = runtime.run_destinations()
        except Exception:  # noqa: BLE001 - no project open yet, or no executor manager
            destinations = []
        for name, label in destinations:
            self.addItem(label, name)
        index = self.findData(current)
        if index >= 0:
            self.setCurrentIndex(index)
        # One destination is not a choice; a combo with a single row is just noise.
        self.setVisible(self.count() > 1)

    def executor_name(self) -> str:
        return str(self.currentData() or DEFAULT_LOCAL_CPU_EXECUTOR)


__all__ = ["RunDestinationCombo", "right_aligned", "split_button"]
