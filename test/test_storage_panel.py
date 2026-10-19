import time

import pytest


def test_storage_panel_reads_checks_and_compacts_off_the_gui_thread(tmp_path, monkeypatch):
    from amdockvs import AMDockVSRuntime  # before PySide: pydantic must finish importing first

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

    from amdockvs.ui.shell.project_summary import _storage_section
    from payload_seed import seed_payload

    app = QApplication.instance() or QApplication(["amdockvs-storage-panel-test"])
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="storage", folder=tmp_path / "storage", description="")
        seed_payload(runtime.workspace.project_db, "ATOM\n", "pdb")
        dlg = QDialog()
        layout = _storage_section(dlg, runtime)
        dlg.setLayout(layout)
        text = next(w for w in dlg.findChildren(QLabel) if w.text() in ("Reading…",) or "volume" in w.text())
        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}

        def wait(fragment: str) -> str:
            deadline = time.monotonic() + 10
            while fragment not in text.text() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            return text.text()

        assert "1 volume(s)" in wait("volume") and "Healthy" in text.text()
        buttons["Check"].click()
        assert not buttons["Compact"].isEnabled()  # one job at a time
        wait("Healthy")
        assert buttons["Compact"].isEnabled()
        assert "1 leftover" in text.text()  # the seeded structure no row points to
        buttons["Repair"].click()
        deadline = time.monotonic() + 10
        while not buttons["Repair"].isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert text.text().endswith("Healthy")
        buttons["Compact"].click()
        assert "volume(s)" in wait("volume(s)")
    finally:
        runtime.shutdown()
