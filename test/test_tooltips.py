"""Long tooltips are broken into lines app-wide: Qt wraps a plain one only past the screen's width."""
from __future__ import annotations

import amdockvs.core.configuration  # noqa: F401  - pydantic before PySide6

from amdockvs.ui.common.tooltips import LINE, WRAP_AFTER, wrapped

COFACTORS = ("Keep cofactors (FMN, HEM, NAD, FAD, ...) in the receptor PDBQT, typed and charged at pH 7 "
             "from their chemical component. Off leaves an empty cofactor pocket the ligand can dock "
             "into — wrong whenever the cofactor walls the site.")


def test_a_long_plain_tooltip_is_broken_into_lines():
    lines = wrapped(COFACTORS).split("\n")
    assert len(lines) > 1 and all(len(line) <= LINE for line in lines)
    assert " ".join(lines) == COFACTORS  # nothing lost or added but the breaks


def test_what_needs_no_wrapping_is_left_alone():
    assert wrapped("") is None
    assert wrapped("x" * WRAP_AFTER) is None
    assert wrapped("Short line\nanother short line") is None
    assert wrapped("<b>Rich</b> " + "word " * 40) is None  # Qt wraps rich text itself


def test_its_own_line_breaks_stay_and_a_long_word_is_not_split():
    text = "First warning\n" + "second " * 20 + "\n" + "a" * 120
    lines = wrapped(text).split("\n")
    assert lines[0] == "First warning"
    assert "a" * 120 in lines


def test_the_filter_shows_a_widget_tooltip_wrapped(monkeypatch):
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication, QCheckBox, QToolTip

    from amdockvs.ui.common import tooltips

    app = QApplication.instance() or QApplication(["tooltips-test"])
    tooltips.install(app)
    tooltips.install(app)  # once only
    shown = []
    monkeypatch.setattr(QToolTip, "showText", lambda _pos, text, *_rest: shown.append(text))
    box = QCheckBox("Include cofactors")
    box.setToolTip(COFACTORS)
    QApplication.sendEvent(box, QHelpEvent(QEvent.Type.ToolTip, QPoint(1, 1), QPoint(1, 1)))
    assert shown == [wrapped(COFACTORS)]


def test_the_filter_wraps_a_table_cell_tooltip_and_leaves_a_short_one_to_qt(monkeypatch):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem, QToolTip

    from amdockvs.ui.common import tooltips

    app = QApplication.instance() or QApplication(["tooltips-test"])
    tooltips.install(app)
    shown = []
    monkeypatch.setattr(QToolTip, "showText", lambda _pos, text, *_rest: shown.append(text))
    table = QTableWidget(2, 1)
    table.resize(300, 200)
    long_item, short_item = QTableWidgetItem("long"), QTableWidgetItem("short")
    long_item.setData(Qt.ItemDataRole.ToolTipRole, COFACTORS)
    short_item.setData(Qt.ItemDataRole.ToolTipRole, "Short")
    table.setItem(0, 0, long_item)
    table.setItem(1, 0, short_item)
    table.show()
    app.processEvents()
    for row in (0, 1):
        pos = table.visualRect(table.model().index(row, 0)).center()
        QApplication.sendEvent(table.viewport(),
                               QHelpEvent(QEvent.Type.ToolTip, pos, table.viewport().mapToGlobal(pos)))
    # The filter took the long cell only. The short one went on to Qt's own path, which calls
    # QToolTip::showText in C++, out of the patch's reach.
    assert shown == [wrapped(COFACTORS)]
    table.close()
