"""The way back out of an exclusion (report Fig2 F2-03).

A finished run reports what it excluded in a dialog. That dialog used to send the user to the
molecule Filter's "Excluded" scope, which does not exist: Filter's Recover re-evaluates
small-molecule criteria and only touches exclusions it owns itself, so a receptor the docking
preparation excluded had no route back through the GUI at all. The route now lives in the dialog
that reports the failure, for any molecule and whatever step excluded it.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QTreeWidget, QWidget

from amdockvs.ui.common.failures import PER_CAUSE, recover_with, show_failures

PREP_REASON = "Docking preparation (ad4): Problem in line 5493: Duplicated ANISOU record"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(["amdockvs-failures-test"])


@pytest.fixture
def parent(app):
    widget = QWidget()
    yield widget
    widget.deleteLater()


def _rows(count=3, reason=PREP_REASON, first_id=1):
    return [(first_id + i, f"mol_{first_id + i}", reason) for i in range(count)]


def _dialog(parent, rows, recover=None):
    dialog = show_failures(parent, "Receptor", rows, recover)
    assert dialog is not None
    return dialog


def _button(dialog):
    found = [b for b in dialog.findChildren(QPushButton) if b.text().startswith("Re-include")]
    return found[0] if found else None


def _tree(dialog) -> QTreeWidget:
    return dialog.findChild(QTreeWidget)


def _hint(dialog) -> str:
    from PySide6.QtWidgets import QLabel
    return dialog.findChild(QLabel).text()


def test_the_dialog_no_longer_sends_the_user_to_a_filter_scope_that_does_not_exist(parent):
    dialog = _dialog(parent, _rows(), recover=lambda ids: len(ids))

    assert "Excluded" not in _hint(dialog) and "Filter" not in _hint(dialog)
    assert "Re-include" in _hint(dialog)


def test_re_include_brings_back_everything_listed_when_nothing_is_selected(parent):
    recovered = []
    dialog = _dialog(parent, _rows(3), recover=lambda ids: recovered.extend(ids) or len(ids))

    button = _button(dialog)
    assert button.text() == "Re-include all 3"
    button.click()

    assert recovered == [1, 2, 3]


def test_a_receptor_excluded_by_preparation_is_recoverable(parent):
    """What the report walked: the one route that has to work for a protein."""
    recovered = []
    rows = [(5, "1d3g_breqanalog-bound_hDHODH", PREP_REASON)]
    dialog = _dialog(parent, rows, recover=lambda ids: recovered.extend(ids) or len(ids))

    _button(dialog).click()

    assert recovered == [5]


def test_a_selected_cause_takes_its_whole_group_including_the_members_it_does_not_list(parent):
    many = _rows(PER_CAUSE + 7)  # more members than the tree shows under one cause
    other = _rows(2, reason="RDKit: Explicit valence for atom # 0 N, 4, is greater than permitted",
                  first_id=1000)
    recovered = []
    dialog = _dialog(parent, many + other, recover=lambda ids: recovered.extend(ids) or len(ids))
    tree = _tree(dialog)

    causes = {tree.topLevelItem(i).text(0).split("  (")[0]: tree.topLevelItem(i)
              for i in range(tree.topLevelItemCount())}
    biggest = causes[max(causes, key=lambda key: len(causes[key].data(0, Qt.UserRole) or []))]
    biggest.setSelected(True)

    assert _button(dialog).text() == f"Re-include {PER_CAUSE + 7} selected"
    _button(dialog).click()
    assert recovered == [row[0] for row in many]  # the unlisted ones too, and not the other cause


def test_a_selected_molecule_is_the_only_one_recovered(parent):
    recovered = []
    dialog = _dialog(parent, _rows(3), recover=lambda ids: recovered.extend(ids) or len(ids))
    tree = _tree(dialog)
    tree.topLevelItem(0).child(1).setSelected(True)

    _button(dialog).click()

    assert recovered == [2]


def test_what_came_back_is_not_offered_again(parent):
    calls = []
    dialog = _dialog(parent, _rows(2), recover=lambda ids: calls.append(ids) or len(ids))
    button = _button(dialog)

    button.click()
    assert calls == [[1, 2]] and "Brought back 2" in _hint(dialog)
    assert not button.isEnabled()  # nothing left to recover

    button.click()  # a second press cannot double-apply
    assert calls == [[1, 2]]


def test_a_failure_to_re_include_is_reported_and_leaves_the_button_usable(parent, monkeypatch):
    shown = []
    monkeypatch.setattr("amdockvs.ui.common.failures.QMessageBox.critical",
                        lambda *args, **kwargs: shown.append(args[-1]))

    def refuse(_ids):
        raise RuntimeError("database is locked")

    dialog = _dialog(parent, _rows(2), recover=refuse)
    _button(dialog).click()

    assert shown == ["database is locked"]
    assert _button(dialog).isEnabled() and "Brought back" not in _hint(dialog)


def test_without_a_recover_the_dialog_only_reports(parent):
    dialog = _dialog(parent, _rows())

    assert _button(dialog) is None
    assert "Re-include" not in _hint(dialog)


def test_no_dialog_without_failures(parent):
    assert show_failures(parent, "Receptor", [], lambda ids: len(ids)) is None


def test_recover_with_clears_the_exclusion_and_refreshes():
    """No Qt: the factory the panels hand to the dialog, against the molecules API it calls."""
    calls, refreshed = [], []
    runtime = SimpleNamespace(molecules=SimpleNamespace(
        set_excluded_state=lambda ids, **kwargs: calls.append((list(ids), kwargs)) or len(list(ids))))

    count = recover_with(runtime, lambda: refreshed.append(True))([7, 9])

    assert count == 2
    assert calls == [([7, 9], {"excluded": False})]  # excluded=False is the whole point
    assert refreshed == [True]  # the panel's table shows them active again


# --- the panels that report failures hand the dialog that route -------------------------------
# Unbound, with a stand-in for `self`: building either panel needs a whole runtime, and what
# matters here is only that the route reaches the dialog and clears the exclusion.

def _panel_hands_recover(module_name: str, call, monkeypatch):
    import importlib

    module = importlib.import_module(module_name)
    captured: dict = {}
    monkeypatch.setattr(module, "show_failures",
                        lambda *args: captured.update(args=args) or None)
    cleared, refreshed = [], []
    runtime = SimpleNamespace(molecules=SimpleNamespace(
        set_excluded_state=lambda ids, **kwargs: cleared.append((list(ids), kwargs)) or len(list(ids))))
    panel = SimpleNamespace(runtime=runtime, refresh=lambda: refreshed.append(True), failures_dialog=None)

    call(panel, [(5, "1d3g_breqanalog-bound_hDHODH", PREP_REASON)])

    recover = captured["args"][3]
    assert callable(recover), "the dialog is opened without a way back out of the exclusion"
    assert recover([5]) == 1
    assert cleared == [([5], {"excluded": False})] and refreshed == [True]


def test_the_preparation_panel_offers_recovery_for_what_it_excluded(monkeypatch):
    from amdockvs.ui.tools.docking.preparation_panel import PreparationPanel

    _panel_hands_recover(
        "amdockvs.ui.tools.docking.preparation_panel",
        lambda panel, rows: PreparationPanel._show_prep_failures(panel, "Receptor", rows),
        monkeypatch,
    )


def test_the_build_panel_offers_recovery_for_what_it_excluded(monkeypatch):
    from amdockvs.ui.tools.molecules.build import MoleculeBuildWidget

    _panel_hands_recover(
        "amdockvs.ui.tools.molecules.build",
        lambda panel, rows: MoleculeBuildWidget._show_failures(panel, rows),
        monkeypatch,
    )
