"""Which catalog table the docking studio borrows for its ligand step.

Two independent questions decide it: the experiment (docking screens the general library,
redocking re-docks the reference cocrystals) and the project (rows in `vs`, shards in
`htpvs`). Only their combination sends the step to the Shards table. Getting this wrong is
silent: the panel would narrow a docking by shard row ids, which are not molecule ids.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401  (pydantic before PySide6)

pytest.importorskip("PySide6")

from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID
from amdockvs.ui.catalog.shards import SHARDS_VIEW_ID
from amdockvs.ui.tools.docking.scope_panel import ScopePanel


class _Panel(ScopePanel):
    def __init__(self, mode, run_kind):
        self.runtime = SimpleNamespace(mode=mode)
        self._kind = run_kind

    def _run_kind(self):
        return self._kind


@pytest.mark.parametrize(
    "mode, run_kind, view_id",
    [
        ("htpvs", "docking", SHARDS_VIEW_ID),
        ("htpvs", "redocking", LIGANDS_VIEW_ID),  # reference ligands are rows in both modes
        ("vs", "docking", LIGANDS_VIEW_ID),
        ("vs", "redocking", LIGANDS_VIEW_ID),
    ],
)
def test_ligand_table_follows_experiment_and_library(mode, run_kind, view_id):
    panel = _Panel(mode, run_kind)
    assert panel._ligand_view_id() == view_id
    assert panel._ligand_scope_is_sharded() is (view_id == SHARDS_VIEW_ID)


def test_no_project_no_shards():
    """`runtime.mode` blows up with no project open; the step must not."""
    panel = _Panel(None, "docking")
    panel.runtime = SimpleNamespace()  # no `.mode` at all
    assert panel._library_is_sharded() is False
