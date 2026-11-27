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

        def said() -> str:  # the line under the buttons: "HH:MM:SS  what the last one did"
            return next((w.text() for w in dlg.findChildren(QLabel) if w.text()[2:3] == ":" and w.text()[:2].isdigit()), "")

        def settle() -> None:
            deadline = time.monotonic() + 10
            while not buttons["Repair"].isEnabled() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)

        assert "1 volume(s)" in wait("volume") and "Healthy" in text.text()
        buttons["Check"].click()
        assert not buttons["Compact"].isEnabled()  # one job at a time
        wait("Healthy")
        assert buttons["Compact"].isEnabled()
        assert "1 leftover" in text.text()  # the seeded structure no row points to
        assert said().endswith("Check: every structure read; 1 leftover item(s), Repair clears them.")
        buttons["Repair"].click()
        settle()
        assert text.text().endswith("Healthy")
        assert said().endswith("Repair: cleared 1 leftover item(s).")
        buttons["Compact"].click()
        settle()
        assert "volume(s)" in text.text() and "  Compact: " in said()
    finally:
        runtime.shutdown()


def test_a_sharded_project_shows_its_library_in_the_project_dialog(tmp_path, monkeypatch):
    """The row counts read zero for a sharded library: its section reads the shard inventory."""
    from amdockvs import AMDockVSRuntime  # before PySide: pydantic must finish importing first

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QDialog, QLabel

    from ms_flow.core.database.project_models import CatalogFile
    from ms_flow.core.data.catalog import activate_generation, create_generation
    from amdockvs.ui.shell.project_summary import _library_section

    app = QApplication.instance() or QApplication(["amdockvs-library-section-test"])
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="library", folder=tmp_path / "library", description="")
        project_db = runtime.workspace.project_db
        generation = create_generation(project_db, step="import", folder=tmp_path)
        activate_generation(project_db, generation)
        with project_db.get_session() as session:
            session.add(CatalogFile(generation_id=generation, path=str(tmp_path / "lib.parquet"),
                                    n_shards=2, n_records=3000, n_active=3000))
            session.commit()
        dlg = QDialog()
        dlg.setLayout(_library_section(dlg, runtime))
        values = dlg.findChildren(QLabel, "summaryCountValue")
        deadline = time.monotonic() + 10
        while values[0].text() == "…" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert [label.text() for label in values] == ["3,000", "2", "0", "0"]  # ligands, shards, docked, hits
        assert any(label.text().startswith("Last step: import") for label in dlg.findChildren(QLabel))
    finally:
        runtime.shutdown()


def test_the_copies_on_queues_are_brought_and_cleaned_from_the_project_dialog(monkeypatch):
    from amdockvs import AMDockVSRuntime  # noqa: F401  (pydantic before PySide)

    pytest.importorskip("PySide6")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

    from amdockvs.ui.shell import project_summary

    app = QApplication.instance() or QApplication(["amdockvs-queues-test"])
    calls = []
    runtime = SimpleNamespace(
        queue_copies=lambda: {"vhpc": {"there": 3, "here_too": 2}},
        bring_from=lambda name: calls.append(("bring", name)) or [1, 2, 3],
        clean_host=lambda name, dry=False, confirm=False: calls.append(("clean", name, dry, confirm)) or [
            {"path": "/w/a.arrows", "verified": True, "removed": not dry},
            {"path": "/w/b.arrows", "verified": False, "removed": not dry and confirm}])
    asked = []
    monkeypatch.setattr(project_summary, "_confirm_clean",
                        lambda dlg, name, plan: asked.append((name, len(plan))) or (True, False))
    dlg = QDialog()
    dlg.setLayout(project_summary._queues_section(dlg, runtime))
    buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}

    def settle(n):
        deadline = time.monotonic() + 10
        while len(calls) < n and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            app.processEvents()

    assert any("3 only there, 2 here too" in w.text() for w in dlg.findChildren(QLabel))
    buttons["Bring here"].click()
    settle(1)
    buttons["Clean vhpc…"].click()
    settle(3)
    assert calls == [("bring", "vhpc"), ("clean", "vhpc", True, False), ("clean", "vhpc", False, False)]
    assert asked == [("vhpc", 2)]


def test_compacting_the_project_merges_its_small_containers(tmp_path, monkeypatch):
    """What campaigns sealed one by one (each small) becomes one container again, off the jobs."""
    from amdockvs import AMDockVSRuntime
    from ms_flow.core.data import catalog
    from container_parts import part, receipt

    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="merge", folder=tmp_path / "merge", description="")
        db = runtime.workspace.project_db
        gen = catalog.create_generation(db, step="import")
        catalog.activate_generation(db, gen)
        with catalog.ContainerWriter(db, tmp_path / "merge" / "made", generation_id=gen, role="prepared:x",
                                     prefix="s:") as writer:
            for i in range(3):
                writer.commit_parts([receipt(part(tmp_path / "parts" / f"{i}.arrows", 8 * i), f"s:{i}")])
                writer.seal()
        assert len(list(catalog.files(db, catalog.shard_spec(role="prepared:x")))) == 3
        assert runtime.merge_containers() == 3
        assert len(list(catalog.files(db, catalog.shard_spec(role="prepared:x")))) == 1
        assert catalog.count(db, catalog.shard_spec(role="prepared:x")) == 3
    finally:
        runtime.shutdown()
