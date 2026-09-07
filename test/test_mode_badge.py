"""The top data toolbar says which mode the project runs in.

The badge is what tells the user that the catalog tables next to it are (or are not) where
the library lives. It reads `runtime.mode`, which is derived from the data. The label mapping
is what is worth testing; whether the badge is *on* the toolbar is asserted in
test_amdock_ui.py, where a real window is built.
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
from PySide6.QtWidgets import QApplication, QLabel

from amdockvs.ui.catalog import LIGANDS_VIEW_ID, MOLECULES_VIEW_ID, SHARDS_VIEW_ID
from amdockvs.ui.catalog.domain_views import LIGAND_ACTIVITY_VIEW_ID
from amdockvs.ui.registry import CATALOG_TABLES
from amdockvs.ui.shell.view_coordinator import ViewCoordinator


class _FakeCentral:
    """Records what the coordinator does to the tabs, without building any."""

    def __init__(self):
        self.closed: list[str] = []

    def close_view(self, view_id):
        self.closed.append(view_id)


def _coordinator(mode: str | None, *, sharded: bool = False, ligand_rows: int = 0) -> ViewCoordinator:
    QApplication.instance() or QApplication(["amdockvs-mode-badge-test"])
    coordinator = ViewCoordinator.__new__(ViewCoordinator)
    coordinator.mode_badge = QLabel()
    coordinator.catalog_actions = {
        view_id: QAction(label) for label, view_id, _icon in CATALOG_TABLES
    }
    coordinator.w = SimpleNamespace(
        runtime=SimpleNamespace(mode=mode),
        _has_active_project=lambda: mode is not None,
        central_widget=_FakeCentral(),
    )
    coordinator._library_shape = lambda: (sharded, ligand_rows)
    coordinator.sync_mode_badge()
    return coordinator


def _badge(mode: str | None) -> QLabel:
    return _coordinator(mode).mode_badge


@pytest.mark.parametrize("mode, text", [("vs", "VS"), ("htpvs", "HTP-VS")])
def test_badge_names_the_mode(mode, text):
    badge = _badge(mode)
    assert badge.text() == text
    assert not badge.isHidden()
    assert "how the ligands were imported" in badge.toolTip()


def test_no_project_no_badge():
    """The welcome screen has no project, so there is no mode to state."""
    assert _badge(None).isHidden()


def test_sharded_project_shows_shards_and_greys_an_empty_ligands():
    """The screening library is not in the Ligands table, so its empty state would lie."""
    coordinator = _coordinator("htpvs", sharded=True, ligand_rows=0)
    actions = coordinator.catalog_actions
    assert actions[SHARDS_VIEW_ID].isVisible()
    assert not actions[LIGANDS_VIEW_ID].isEnabled()
    # No renaming, ever: a table means the same thing in every project.
    assert actions[LIGANDS_VIEW_ID].text() == "Ligands"
    assert actions[MOLECULES_VIEW_ID].isVisible()
    assert actions[LIGAND_ACTIVITY_VIEW_ID].isVisible()


def test_reference_ligands_light_the_ligands_table_up():
    """Cocrystals (redocking, QSAR) and promoted hits are rows, campaign or not."""
    coordinator = _coordinator("htpvs", sharded=True, ligand_rows=3)
    assert coordinator.catalog_actions[LIGANDS_VIEW_ID].isEnabled()


@pytest.mark.parametrize("mode", ["vs", None])
def test_no_shards_table_without_a_sharded_library(mode):
    coordinator = _coordinator(mode)
    assert not coordinator.catalog_actions[SHARDS_VIEW_ID].isVisible()
    assert coordinator.catalog_actions[LIGANDS_VIEW_ID].isEnabled()
    assert coordinator.w.central_widget.closed == [SHARDS_VIEW_ID]
