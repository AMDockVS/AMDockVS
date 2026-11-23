"""`set_widget_value` puts a recorded value in, or leaves the widget as it is: never a value the
widget picks instead (project/submitted.py restores)."""
import math

import pytest


@pytest.fixture
def qt(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _spins():
    from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox

    ph = QDoubleSpinBox()
    ph.setRange(0.0, 14.0)
    ph.setDecimals(1)
    ph.setValue(7.4)
    attempts = QSpinBox()
    attempts.setRange(1, 50)
    attempts.setValue(1)
    return ph, attempts


@pytest.mark.parametrize("value", [True, False, 999, -1, 14.5, "6.5", "unreadable", math.nan, math.inf, 6.55, [6.5]])
def test_a_number_the_spin_box_does_not_offer_leaves_it_as_it_is(qt, value):
    from amdockvs.ui.common.widgets import set_widget_value

    ph, attempts = _spins()
    assert set_widget_value(ph, value) is False and ph.value() == 7.4
    assert set_widget_value(attempts, value) is False and attempts.value() == 1


def test_a_number_it_offers_goes_in(qt):
    from amdockvs.ui.common.widgets import set_widget_value

    ph, attempts = _spins()
    assert set_widget_value(ph, 6.5) and ph.value() == 6.5
    assert set_widget_value(ph, 6) and ph.value() == 6.0  # JSON writes 6.0 as 6 too
    assert set_widget_value(attempts, 50) and attempts.value() == 50
    assert set_widget_value(attempts, 3.0) and attempts.value() == 3
    assert set_widget_value(attempts, 2.5) is False and attempts.value() == 3


def test_combos_and_check_boxes_take_only_what_they_offer(qt):
    from PySide6.QtWidgets import QCheckBox, QComboBox

    from amdockvs.ui.common.widgets import set_widget_value

    combo = QComboBox()
    combo.addItem("one", 1)
    combo.addItem("pair", ("a.xml", "b.xml"))
    combo.addItem("name", "openbabel")
    combo.setCurrentIndex(2)
    assert set_widget_value(combo, True) is False and combo.currentIndex() == 2  # True == 1 in Python
    assert set_widget_value(combo, "renamed-since") is False and combo.currentIndex() == 2
    assert set_widget_value(combo, ["a.xml", "b.xml"]) and combo.currentIndex() == 1
    assert set_widget_value(combo, 1) and combo.currentIndex() == 0
    box = QCheckBox()
    assert set_widget_value(box, 1) is False and not box.isChecked()
    assert set_widget_value(box, True) and box.isChecked()
