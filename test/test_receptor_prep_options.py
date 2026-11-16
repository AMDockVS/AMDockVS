"""The receptor preparation options the panel offers, and the ones it keeps to itself.

Keeping waters or cofactors is not finished end to end, so those two are built and wired but
hidden. Hidden, they read as unchecked, which is how a receptor is prepared today: dry, with
the cofactor pocket empty. The test is here so unhiding them is a deliberate act with a
failing test behind it, not an accident of a layout change.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from types import SimpleNamespace

import pytest

import amdockvs.runtime  # noqa: F401 - pydantic before PySide6 (see test_diagram_follows_results)
from PySide6.QtWidgets import QApplication, QFormLayout, QWidget

from amdockvs.ui.tools.docking.preparation_panel import PreparationPanel


class _Host(PreparationPanel, QWidget):
    """The panel's builders want a QWidget host, as DockingStudioWidget is.

    `_in_scroll` is the studio's own wrapper; the page inside it is what this is about.
    """

    def _in_scroll(self, inner: QWidget) -> QWidget:
        return inner


@pytest.fixture
def page():
    app = QApplication.instance() or QApplication(["amdockvs-prep-options-test"])
    host = _Host()
    yield host, host._build_receptor_general_prep_page()
    host.close()
    app.processEvents()


def _form(page: QWidget) -> QFormLayout:
    [form] = page.findChildren(QFormLayout)
    return form


def test_force_re_prepare_is_offered_and_the_unfinished_options_are_not(page):
    host, built = page
    form = _form(built)
    visible = {form.itemAt(row, QFormLayout.SpanningRole).widget().text()
               for row in range(form.rowCount())
               if form.isRowVisible(row)}
    assert visible == {"Force re-prepare"}


def test_the_hidden_options_still_answer_as_a_dry_receptor(page):
    host, _ = page
    # Still there to be read: `prepare_receptors` passes both on every run and every saved
    # workflow, and unchecked is the dry receptor the step prepares today.
    assert host.keep_waters_receptors.isChecked() is False
    assert host.keep_cofactors_receptors.isChecked() is False
    assert host.keep_waters_receptors.isVisible() is False
    assert host.keep_cofactors_receptors.isVisible() is False
