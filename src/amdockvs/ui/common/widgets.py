"""Small shared widget helpers. Nothing here is a component — just Qt spellings we repeat."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto new lines (Qt's flow-layout example, trimmed)."""

    def __init__(self, parent=None, spacing: int = 3):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, *, apply: bool) -> int:
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, line_height = rect.x(), y + line_height + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


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


class ChipsSelect(QWidget):
    """Generic chips + combo multi-select (same shape as ms_table's Order/Filter bars): the combo
    offers the not-yet-picked items, each pick becomes a removable chip. Used for both the chain
    selection and the reference-ligand selection. Chips wrap onto new lines; ``heightChanged``
    asks the table to fit the row.
    """

    changed = Signal()
    heightChanged = Signal()

    # Rounded pill chips; palette() roles so they follow the active theme.
    _STYLE = """
        QFrame[chip_objet="true"] {
            border: 1px solid palette(mid); border-radius: 9px; background: palette(button);
        }
        QFrame[chip_objet="true"] QLabel { border: none; background: transparent; }
        QFrame[chip_objet="true"] QToolButton { border: none; background: transparent; padding: 0; }
        QFrame[chip_objet="true"] QToolButton:hover { color: palette(highlight); }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(self._STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        self._chips = QWidget(self)
        self._chips_layout = FlowLayout(self._chips, spacing=4)
        layout.addWidget(self._chips, 1)
        self._combo = QComboBox(self)
        self._combo.setFixedWidth(46)
        self._combo.currentIndexChanged.connect(self._on_add)
        layout.addWidget(self._combo, 0, Qt.AlignmentFlag.AlignTop)
        self._options: list[str] = []
        self._selected: list[str] = []
        self._last_height = 0

    def sizeHint(self) -> QSize:
        # The table sizes rows from the cell widget's sizeHint, so report the wrapped height.
        # The combo sits below the chips (vertical layout), so the chips get the full inner width.
        margins = self.layout().contentsMargins()
        chips_width = max(1, self.width() - margins.left() - margins.right())
        height = self._chips_layout.heightForWidth(chips_width)
        if not self._combo.isHidden():
            height += self.layout().spacing() + self._combo.sizeHint().height()
        return QSize(super().sizeHint().width(), height + margins.top() + margins.bottom())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._check_height()

    def _check_height(self) -> None:
        height = self.sizeHint().height()
        if height != self._last_height:
            self._last_height = height
            self.heightChanged.emit()

    def set_items(self, items, *, selected=None) -> None:
        self._options = [str(item) for item in items]
        if selected is None:
            self._selected = list(self._options)
        else:
            self._selected = [str(item) for item in selected if str(item) in self._options]
        self._rebuild()

    def selected_items(self) -> list[str]:
        return list(self._selected)

    def _rebuild(self) -> None:
        while self._chips_layout.count():
            widget = self._chips_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for item in self._selected:
            self._chips_layout.addWidget(self._make_chip(item))
        # New chips only get a size once Qt shows them on the next event-loop pass.
        QTimer.singleShot(0, self, self._check_height)
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("＋", None)
        for option in self._options:
            if option not in self._selected:
                self._combo.addItem(option, option)
        self._combo.setCurrentIndex(0)
        # Nothing left to add -> no combo, so a fully-picked row isn't taller than its chips.
        self._combo.setHidden(self._combo.count() == 1)
        self._combo.blockSignals(False)

    def _make_chip(self, item: str) -> QFrame:
        chip = QFrame(self)
        chip.setProperty("chip_objet", True)
        row = QHBoxLayout(chip)
        row.setContentsMargins(8, 1, 4, 1)
        row.setSpacing(2)
        row.addWidget(QLabel(item, chip))
        remove = QToolButton(chip)
        remove.setText("×")
        remove.setAutoRaise(True)
        remove.setMaximumSize(20, 20)
        remove.clicked.connect(lambda: self._remove(item))
        row.addWidget(remove)
        return chip

    def _on_add(self, index: int) -> None:
        item = self._combo.itemData(index)
        if item is None or item in self._selected:
            return
        self._selected.append(item)
        self._rebuild()
        self.changed.emit()

    def _remove(self, item: str) -> None:
        if item in self._selected:
            self._selected.remove(item)
            self._rebuild()
            self.changed.emit()


__all__ = ["ChipsSelect", "FlowLayout", "right_aligned", "RunDestinationCombo", "split_button", "WrapCombo"]
