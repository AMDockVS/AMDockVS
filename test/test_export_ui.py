import time
import zipfile
from types import SimpleNamespace

import pytest


def test_export_action_writes_the_selection_off_the_gui_thread(tmp_path, monkeypatch):
    from amdockvs import AMDockVSRuntime  # before PySide: pydantic must finish importing first

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

    from amdockvs.ui.catalog.export import export_molecules
    from payload_seed import seed_payload

    app = QApplication.instance() or QApplication(["amdockvs-export-ui-test"])
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="export", folder=tmp_path / "export", description="")
        store = runtime.workspace.project_db
        rows = [SimpleNamespace(name="rec", current_payload_id=None, stored_payload_id=seed_payload(store, "ATOM\n", "pdb")),
                SimpleNamespace(name="lig", current_payload_id=seed_payload(store, "ROOT\n", "pdbqt"), stored_payload_id=None)]
        target = tmp_path / "out.zip"
        told = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
        monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: told.append(text))

        export_molecules(QWidget(), runtime, rows)
        deadline = time.monotonic() + 10
        while not told and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        assert told and "Exported 2" in told[0]
        with zipfile.ZipFile(target) as archive:
            assert sorted(archive.namelist()) == ["lig.pdbqt", "rec.pdb"]
    finally:
        runtime.shutdown()
