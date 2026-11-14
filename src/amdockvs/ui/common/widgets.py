"""Small shared widget helpers. Nothing here is a component — just Qt spellings we repeat."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.ui.resources.icons import icon as load_icon

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


def help_button(parent: QWidget, tooltip: str, *, name: str) -> QToolButton:
    """Small "?" button: hover shows `tooltip`, and a click shows it too (no wait for hover)."""
    button = QToolButton(parent)
    button.setIcon(load_icon("help.svg"))
    button.setIconSize(QSize(16, 16))
    button.setAutoRaise(True)
    button.setFixedSize(22, 22)
    button.setStyleSheet("QToolButton { padding: 0; }")
    button.setAccessibleName(name)
    button.setToolTip(tooltip)
    button.clicked.connect(
        lambda: QToolTip.showText(button.mapToGlobal(button.rect().bottomLeft()), button.toolTip(), button)
    )
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


class QueueResources(QPushButton):
    """What the jobs sent to a cluster's queue ask of it instead of what its executor asks: CPUs,
    time... Kept in `runtime.queue_asks` for the session, so every tool sending jobs to that
    queue asks the same, and never saved (the executor's own are edited in Executors).
    `queue()` says whether `destination` is a queue, where this means something."""

    # ponytail: the Slurm fields of ms_flow.site_script.RESOURCES; Grid Engine asks through its setup
    _FIELDS = (("partition", "Partition"), ("account", "Account"), ("nodes", "Nodes"), ("cpus", "CPUs per node"),
               ("gpus", "GPUs per node"), ("memory_gb", "Memory per node (GB)"), ("time", "Time limit"))

    def __init__(self, parent: QWidget, runtime, destination: RunDestinationCombo) -> None:
        super().__init__("Queue resources…", parent)
        self._runtime, self._destination = runtime, destination
        self.setToolTip("What each job sent to the cluster's queue asks of it, from every tool, until the app "
                        "closes. CPUs per node are what one node gives the run's tasks, shared among them.")
        self.clicked.connect(self._edit)

    def _executor(self):
        try:
            executor = self._runtime.workspace.settings_manager.settings.executors.get(self._destination.executor_name())
        except Exception:  # noqa: BLE001 - no settings yet
            return None
        return executor if executor is not None and executor.kind == "queue" else None

    def queue(self) -> bool:
        return self._executor() is not None

    def asks(self) -> dict:
        """What differs from the executor's own, for the queue chosen; {} anywhere else."""
        return dict(self._runtime.queue_asks.get(self._destination.executor_name(), {})) if self.queue() else {}

    def _edit(self) -> None:
        executor, name = self._executor(), self._destination.executor_name()
        if executor is None:
            return
        asked = self._runtime.queue_asks
        now = {field: getattr(executor, field) for field, _ in self._FIELDS} | asked.get(name, {})
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Resources on {name}")
        dialog.setMinimumWidth(380)
        form, editors = QFormLayout(dialog), {}
        for field, label in self._FIELDS:
            if isinstance(now[field], int):
                editor = QSpinBox(dialog)
                editor.setRange(0, 100_000)
                editor.setSpecialValueText("Queue default")
                editor.setValue(now[field])
            else:
                editor = QLineEdit(str(now[field]), dialog)
                editor.setPlaceholderText("Queue default" if field != "time" else "Queue default (e.g. 12:00:00)")
            editors[field] = editor
            form.addRow(label, editor)
        # Warned, not refused: a site may give whole nodes whatever is asked.
        warning = QLabel("", dialog)
        warning.setWordWrap(True)
        form.addRow(warning)

        def warn() -> None:
            warning.setText("" if editors["cpus"].value() else
                            "With CPUs left to the queue's default, most sites give a job one CPU: "
                            "its tasks then run one after another.")

        editors["cpus"].valueChanged.connect(lambda _value: warn())
        warn()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        restore.setText("The executor's")
        restore.clicked.connect(lambda: (asked.pop(name, None), dialog.reject()))
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = {field: editor.value() if isinstance(editor, QSpinBox) else editor.text().strip()
                      for field, editor in editors.items()}
            asked[name] = {field: value for field, value in values.items() if value != getattr(executor, field)}
        self.refresh()

    def refresh(self) -> None:
        self.setText("Queue resources (changed)…" if self.asks() else "Queue resources…")

    def showEvent(self, event) -> None:
        self.refresh()  # another tool may have changed them
        super().showEvent(event)


def add_run_on(advanced, runtime) -> RunDestinationCombo:
    """The "Run on" row of a tool's advanced form, and under it "Queue resources…" while what is
    chosen is a cluster's queue. Rows hidden, label too, when there is nothing to choose."""
    destination = RunDestinationCombo(advanced, runtime)
    advanced.add_row("Run on", destination)
    resources = QueueResources(advanced, runtime, destination)
    advanced.add_row("", resources)
    destination.resources = resources

    def show() -> None:
        advanced.form.setRowVisible(destination, destination.count() > 1)
        advanced.form.setRowVisible(resources, resources.queue())
        resources.refresh()

    destination.currentIndexChanged.connect(lambda _index: show())
    show()
    return destination


class WrapCombo(QComboBox):
    """Combo whose closed face word-wraps its text over two lines ("Biological assembly 1")."""

    _FLAGS = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap

    def _field(self, option) -> QRect:
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, self
        ).adjusted(4, 0, -2, 0)

    def sizeHint(self) -> QSize:
        # The table sizes the row from this: tall enough for the wrapped text at the current width.
        hint = super().sizeHint()
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        width = max(1, self._field(option).width())
        text_height = self.fontMetrics().boundingRect(QRect(0, 0, width, 10_000), int(self._FLAGS), self.currentText()).height()
        return QSize(hint.width(), max(hint.height(), text_height + 8))

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text, option.currentText = option.currentText, ""
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawItemText(
            self._field(option), int(self._FLAGS), option.palette, self.isEnabled(), text, QPalette.ColorRole.ButtonText
        )


__all__ = ["add_run_on", "QueueResources", "right_aligned", "RunDestinationCombo", "split_button", "WrapCombo"]


class LigandSources(QWidget):
    """"Ligands from": the table's rows, the shard library, or both — one box each, its count
    beside it with a Show link. Both start ticked; the host shows it only in a sharded project.
    """

    changed = Signal()
    show_requested = Signal(str)  # "rows" or "shards"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.boxes = {"rows": QCheckBox("Table", self), "shards": QCheckBox("Shards", self)}
        self.counts: dict[str, QLabel] = {}
        for source, box in self.boxes.items():
            box.setChecked(True)
            box.toggled.connect(lambda _=False: self.changed.emit())
            count = QLabel(self)
            count.linkActivated.connect(lambda _href, source=source: self.show_requested.emit(source))
            self.counts[source] = count
            row = QHBoxLayout()
            row.addWidget(box)
            row.addWidget(count, 1)
            layout.addLayout(row)
        self.set_counts("Ligand rows", "Screening library")

    def sources(self) -> set[str]:
        return {source for source, box in self.boxes.items() if box.isChecked()}

    def set_rows_available(self, available: bool) -> None:
        """Ticked whenever the table turns available, unticked when it doesn't; the user's own
        pick stands in between."""
        box = self.boxes["rows"]
        if box.isEnabled() != bool(available):
            box.setChecked(bool(available))
        box.setEnabled(bool(available))

    def set_counts(self, rows: str, shards: str) -> None:
        show = " · <a href='show'>Show</a>"
        self.counts["rows"].setText(rows + show)
        self.counts["shards"].setText(shards + show)
