"""Missing receptor structures offer Build without preventing the existing skip path."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pytest
from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from ms_flow.query import db_pages

from amdockvs.docking.preparation.jobs import scope_spec
from amdockvs.models import BindingSite, MoleculeRecord
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import BUILD_ID, DOCKING_VIEW_ID
from payload_seed import seed_payload


@pytest.fixture
def receptor_studio(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    window = None
    try:
        runtime.create_project(name="receptor_3d", folder=tmp_path / "project", description="")
        store = runtime.workspace.project_db
        payload = seed_payload(store, "ATOM      1  CA  GLY A   1       0.000   0.000   0.000  1.00 90.00           C\nEND\n", "pdb")
        with store.get_session() as session:
            rows = [
                MoleculeRecord(name=name, molecule_type="protein", is_receptor=True,
                               has_3d=has_3d, current_payload_id=payload if has_3d else None)
                for name, has_3d in [("structured", True), ("sequence", False), ("outside scope", False)]
            ]
            session.add_all(rows)
            session.commit()
            for row in rows:
                session.refresh(row)
            ids = [row.id for row in rows]
            session.add(BindingSite(molecule_id=ids[0], is_active=True,
                                    center_x=0, center_y=0, center_z=0, size_x=20, size_y=20, size_z=20))
            session.commit()
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        window.selection.set("receptor", ids[:2])
        studio = window.tools.open_tool(DOCKING_VIEW_ID)
        studio.stepper.set_current_index(2)
        app.processEvents()
        submitted, followed = [], []

        def submit(_job, *, params, **kwargs):
            # Keep the real API and its preparation scope; don't run an external preparer.
            submitted.append(params)
            return f"prep-{len(submitted)}"

        monkeypatch.setattr(runtime.docking, "_submit_preparation_job", submit)
        monkeypatch.setattr(studio, "_follow_prep", lambda *args, **kwargs: followed.append(args))
        yield SimpleNamespace(app=app, runtime=runtime, window=window, studio=studio,
                              ids=ids, submitted=submitted, followed=followed)
    finally:
        if window is not None:
            window.close()
        # The panel reads its scope off the GUI thread. Shutting the runtime down while one of
        # those reads is still in the project database leaves the next test contending with it.
        QThreadPool.globalInstance().waitForDone(10_000)
        runtime.shutdown()
        app.processEvents()


def _answer_dialog(scene, action, *, screenshot=None):
    seen = []

    def answer():
        box = scene.app.activeModalWidget()
        assert isinstance(box, QMessageBox)
        seen.append((box.text(), box.informativeText()))
        if screenshot is not None:
            box.grab().save(str(screenshot))
        if action == "escape":
            QTest.keyClick(box, Qt.Key_Escape)
        else:
            next(button for button in box.buttons() if button.text() == action).click()

    QTimer.singleShot(0, answer)
    return seen


def test_continue_warns_about_only_the_selected_sequence_and_prepares_3d_receptors(receptor_studio):
    scene = receptor_studio
    seen = _answer_dialog(scene, "Continue and skip", screenshot="/tmp/amdockvs-receptor-missing-3d.png")
    scene.studio._prepare_receptors()
    assert len(seen) == 1 and "1 receptor(s)" in seen[0][0]
    assert "will be skipped" in seen[0][0]
    assert "Build → Proteins" in seen[0][1]
    assert scene.submitted and scene.followed
    for params in scene.submitted:
        fed = [row["id"] for row in db_pages(scene.runtime.workspace.project_db,
                                            scope_spec(params, entity_kind="receptor"))]
        assert fed == [scene.ids[0]]
    assert scene.window.selection.get("receptor") == scene.ids[:2]
    assert scene.studio.stepper.current_index == 3
    for molecule_id in scene.ids:
        assert not scene.runtime.molecules.get(molecule_id).excluded


def test_build_choice_opens_proteins_without_submitting_preparation(receptor_studio):
    scene = receptor_studio
    seen = _answer_dialog(scene, "Open Build → Proteins…")
    scene.studio._prepare_receptors()
    assert len(seen) == 1 and not scene.submitted and not scene.followed
    assert scene.window.tools.active_tool == BUILD_ID
    build = scene.window.tools.widgets[BUILD_ID]
    assert build.tabs.currentWidget() is build.proteins_tab
    assert scene.window.selection.get("receptor") == scene.ids[:2]
    assert scene.runtime.molecules.count(build._scope()) == 2


def test_a_fully_structured_scope_does_not_show_an_alert(receptor_studio, monkeypatch):
    scene = receptor_studio
    scene.window.selection.set("receptor", [scene.ids[0]])
    monkeypatch.setattr(scene.studio, "_ask_missing_receptor_3d",
                        lambda _count: pytest.fail("A 3D receptor should not produce an alert"))
    scene.studio._prepare_receptors()
    assert scene.submitted and scene.studio.stepper.current_index == 3


def test_escape_keeps_the_existing_skip_path_for_an_all_sequence_scope(receptor_studio):
    scene = receptor_studio
    scene.window.selection.set("receptor", [scene.ids[1]])
    seen = _answer_dialog(scene, "escape")
    scene.studio._prepare_receptors()
    assert len(seen) == 1 and scene.submitted and scene.followed
    for params in scene.submitted:
        assert list(db_pages(scene.runtime.workspace.project_db,
                             scope_spec(params, entity_kind="receptor"))) == []
    assert scene.window.selection.get("receptor") == [scene.ids[1]]
    assert scene.studio.stepper.current_index == 3
