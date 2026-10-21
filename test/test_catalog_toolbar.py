"""Which buttons the top data toolbar shows, read off the project's data.

Shards is contextual (there while the project holds shards), Complexes is ephemeral (there
while its tab is open), and Ligands greys out when a sharded project has no rows in it. The
coordinator is built bare here; the real toolbar is asserted in test_amdock_ui.py.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401  (pydantic before PySide6)

pytest.importorskip("PySide6")
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from amdockvs.ui.registry import CATALOG_TABLES, CONTEXTUAL_VIEWS, STANDING_DATA_VIEWS
from amdockvs.ui.shell.view_coordinator import ViewCoordinator
from amdockvs.ui.view_ids import (
    COMPLEX_PAIRS_VIEW_ID,
    LIGANDS_VIEW_ID,
    LIGAND_ACTIVITY_VIEW_ID,
    MOLECULES_VIEW_ID,
    SHARDS_VIEW_ID,
)


class _FakeCentral:
    """Records what the coordinator does to the tabs, without building any."""

    def __init__(self):
        self.closed: list[str] = []

    def close_view(self, view_id):
        self.closed.append(view_id)


def _coordinator(*, sharded: bool = False, ligand_rows: int = 0) -> ViewCoordinator:
    QApplication.instance() or QApplication(["amdockvs-catalog-toolbar-test"])
    coordinator = ViewCoordinator.__new__(ViewCoordinator)
    entries = (*CATALOG_TABLES, *CONTEXTUAL_VIEWS, *(e for g in STANDING_DATA_VIEWS for e in g))
    coordinator.catalog_actions = {view_id: QAction(label, checkable=True) for label, view_id, _icon in entries}
    coordinator.contextual_chrome = (QAction("gap"),)
    coordinator.w = SimpleNamespace(central_widget=_FakeCentral())
    coordinator._library_shape = lambda: (sharded, ligand_rows)
    coordinator.sync_catalog_availability()
    return coordinator


def test_sharded_project_shows_shards_and_greys_an_empty_ligands():
    """The screening library is not in the Ligands table, so its empty state would lie."""
    coordinator = _coordinator(sharded=True, ligand_rows=0)
    actions = coordinator.catalog_actions
    assert actions[SHARDS_VIEW_ID].isVisible()
    assert not actions[LIGANDS_VIEW_ID].isEnabled()
    # No renaming, ever: a table means the same thing in every project.
    assert actions[LIGANDS_VIEW_ID].text() == "Ligands"
    assert actions[MOLECULES_VIEW_ID].isVisible()
    assert actions[LIGAND_ACTIVITY_VIEW_ID].isVisible()


def test_reference_ligands_light_the_ligands_table_up():
    """Cocrystals (redocking, QSAR) and promoted hits are rows, shards or not."""
    coordinator = _coordinator(sharded=True, ligand_rows=3)
    assert coordinator.catalog_actions[LIGANDS_VIEW_ID].isEnabled()


def test_no_shards_table_without_a_sharded_library():
    coordinator = _coordinator()
    assert not coordinator.catalog_actions[SHARDS_VIEW_ID].isVisible()
    assert coordinator.catalog_actions[LIGANDS_VIEW_ID].isEnabled()
    assert coordinator.w.central_widget.closed == [SHARDS_VIEW_ID]


def test_complexes_button_lives_only_while_its_tab_is_open():
    """Ephemeral contextual view: the tab brings the button; closing it takes it away."""
    coordinator = _coordinator()
    complexes, gap = coordinator.catalog_actions[COMPLEX_PAIRS_VIEW_ID], coordinator.contextual_chrome[0]
    complexes.setVisible(False)
    coordinator._sync_contextual_chrome()
    assert not gap.isVisible()  # Shards hidden too: no empty group
    coordinator.sync_catalog_action(COMPLEX_PAIRS_VIEW_ID, True)
    assert complexes.isVisible() and complexes.isChecked() and gap.isVisible()
    coordinator.sync_catalog_action(COMPLEX_PAIRS_VIEW_ID, False)
    assert not complexes.isVisible() and not gap.isVisible()


def test_shards_button_survives_its_tab_closing():
    coordinator = _coordinator(sharded=True)
    shards = coordinator.catalog_actions[SHARDS_VIEW_ID]
    coordinator.sync_catalog_action(SHARDS_VIEW_ID, False)
    assert shards.isVisible() and not shards.isChecked()
