"""Feedback while a job runs: the submit notification + the "fill the viewport once, then only counters"
refresh policy. Lives in its own module because test_amdock_ui.py is currently unimportable
(stale symbol), and it imports amdockvs (pydantic) before PySide6 so shiboken's feature loader
doesn't trip pydantic's lazy migration shim.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.runtime import AMDockVSRuntime

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from amdockvs.ui.catalog import LIGANDS_VIEW_ID
from amdockvs.ui.main_window import AMDockVSMainWindow


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_dialog_on_submit_and_single_row_load_per_job_run(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_amdock_runtime import _make_smiles_file

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="ui_refresh", folder=tmp_path / "ui_refresh", description="refresh policy")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        ligands_file = tmp_path / "toast.smi"
        _make_smiles_file(ligands_file, count=3)
        job_ids = runtime.loader.load_ligands([ligands_file], batch_size=2, executor_name="thread")
        app.processEvents()
        assert job_ids
        # Submit posts to the notification log and raises the menu-bar bell count.
        assert "started" in window._notifications[-1].title.lower()
        assert window._notification_bell.text().strip() == "1"
        runtime.wait_for_jobs(job_ids, timeout_s=120)

        view = window.central_widget.open_or_focus_view(LIGANDS_VIEW_ID)
        app.processEvents()
        calls = {"rows": 0, "counts": 0}
        monkeypatch.setattr(
            view, "ensure_viewport_filled",
            lambda force=False: (calls.__setitem__("rows", calls["rows"] + 1), True)[1],
        )
        monkeypatch.setattr(view, "refresh_counts", lambda: (calls.__setitem__("counts", calls["counts"] + 1), True)[1])

        window._on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=1, jobs=[]))
        assert window._view_refresh_timer.isActive()
        window.pymol_dock = SimpleNamespace(_amdock_interacting=True)
        window._refresh_current_view_in_background()
        assert calls == {"rows": 0, "counts": 0}
        window.pymol_dock = None
        for _ in range(4):
            window._refresh_current_view_in_background()
        # Rows loaded once; every later tick is counters-only.
        assert calls == {"rows": 1, "counts": 3}

        # Job run ends: one final count, timer stopped, and the next run may load rows again.
        window._on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=0, jobs=[]))
        assert not window._view_refresh_timer.isActive()
        assert calls["counts"] == 4

        # A new run starts from scratch, so the view loads rows once again.
        window._on_project_snapshot_updated(SimpleNamespace(has_project=True, jobs_active=2, jobs=[]))
        assert window._rows_loaded_views == set()
        window._refresh_current_view_in_background()
        assert calls["rows"] == 2
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
