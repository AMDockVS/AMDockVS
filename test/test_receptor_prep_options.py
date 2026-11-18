"""The receptor preparation options the panel offers, and the one it keeps to itself.

"Include cofactors" is offered: ms_adtools types and charges a kept cofactor from its
component at pH 7, so it reaches the PDBQT as real chemistry (aromatic ring, ionized phosphate)
rather than a geometry guess. "Charge metal ions" is offered, off. "Include structural waters" is still built and wired but hidden — a kept water
gets whatever ms_adtools makes of it, and the TIP3P charges its tooltip promises do not exist.
Hidden, it reads as unchecked, which is how a receptor is prepared today: dry.

The test is here so unhiding the water box is a deliberate act with a failing test behind it,
not an accident of a layout change.
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


def test_cofactors_are_offered_and_the_unfinished_water_option_is_not(page):
    host, built = page
    form = _form(built)
    visible = {form.itemAt(row, QFormLayout.SpanningRole).widget().text()
               for row in range(form.rowCount())
               if form.isRowVisible(row)}
    assert visible == {"Force re-prepare", "Include cofactors", "Charge metal ions"}


def test_cofactors_are_off_by_default(page):
    host, _ = page
    # Offered, not imposed: an existing project re-prepares to the same PDBQT it had until
    # someone asks for the cofactor, so turning this on is the user's call.
    assert host.keep_cofactors_receptors.isChecked() is False
    assert host.ion_charges_receptors.isChecked() is False
    # isHidden(), not isVisible(): nothing here is shown, so isVisible() is False for every
    # widget either way and would assert nothing.
    assert host.keep_cofactors_receptors.isHidden() is False


def test_the_hidden_water_option_still_answers_as_a_dry_receptor(page):
    host, _ = page
    # Still there to be read: `prepare_receptors` passes it on every run and every saved
    # workflow, and unchecked is the dry receptor the step prepares today.
    assert host.keep_waters_receptors.isChecked() is False
    assert host.keep_waters_receptors.isHidden() is True
