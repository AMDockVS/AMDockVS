"""The 2D dock shows saved diagrams: a click computes nothing. A pose without one waits in the
widget behind Build diagram, which builds it; the dock saves what comes back."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_the_dock_draws_saved_diagrams_and_never_builds_one(tmp_path):
    pytest.importorskip("meeko")
    from meeko import PDBQTMolecule, RDKitMolCreate
    from PySide6.QtWidgets import QApplication, QMainWindow
    from rdkit import Chem

    from amdockvs.docking.results.diagram import diagram_path_for
    from amdockvs.ui.tools.docking.diagram_dock import InteractionDiagramDock
    from ms_components.ms_dockwidget.widget import DockManager

    data = Path(__file__).resolve().parent / "data"
    mol = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule.from_file(str(data / "1s63_ligand.pdbqt"), skip_typing=True))[0]
    pose = tmp_path / "12.sdf"
    pose.write_text(Chem.MolToMolBlock(mol) + "$$$$\n")
    hit = SimpleNamespace(result_id=1, ligand_name="lig", pose_payload_id=12, receptor_payload_id=7,
                          output_path=pose, receptor_path=data / "1s63_zn_site.pdbqt")

    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    config = SimpleNamespace(get_value=lambda _key: SimpleNamespace(model_dump=dict))
    dock = InteractionDiagramDock("2D", DockManager(window), runtime=SimpleNamespace(contact_map_configuration=config),
                                  parent=window)
    dock.show_hit(hit, 1)
    view = dock.view
    assert dock.stack.currentWidget() is view and view._pages.currentIndex() == 1  # the call to action
    assert "pose 1" in view._prompt_label.text() and view.diagram is None  # held, not read
    assert not diagram_path_for(pose, 1, suffix=".json").exists()  # nothing computed by the click

    built = []
    dock.diagram_built.connect(built.append)
    view._build_button.click()  # the widget builds; the dock saves what it returns
    import time

    deadline = time.monotonic() + 60
    while not built and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert built == [1] and diagram_path_for(pose, 1, suffix=".json").exists()
    assert view._pages.currentWidget() is view._view and "interactions" in dock.status.text()
    dock.reload()  # the saved document draws again without building
    assert dock.stack.currentWidget() is view and view._pages.currentWidget() is view._view
    window.close()


def test_results_build_opens_the_diagram_dock_for_the_current_pose(tmp_path, monkeypatch):
    pytest.importorskip("meeko")
    from meeko import PDBQTMolecule, RDKitMolCreate
    from PySide6.QtWidgets import QApplication
    from rdkit import Chem
    import time

    from amdockvs.runtime import AMDockVSRuntime
    from amdockvs.ui.main_window import AMDockVSMainWindow
    from amdockvs.ui.common.async_query import _ACTIVE_TASKS
    from amdockvs.ui.view_ids import COMPLEXES_VIEW_ID, LIGANDS_VIEW_ID
    from amdockvs.docking.results.diagram import diagram_path_for

    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    app = QApplication.instance() or QApplication([])

    def wait(done):
        deadline = time.monotonic() + 45
        while not done() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert done()

    data = Path(__file__).resolve().parent / "data"
    mol = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule.from_file(str(data / "1s63_ligand.pdbqt"), skip_typing=True))[0]
    pose = tmp_path / "12.sdf"
    pose.write_text(Chem.MolToMolBlock(mol) + "$$$$\n")
    hit = SimpleNamespace(result_id=1, ligand_name="lig", pose_rank=2, pose_payload_id=12,
                          receptor_payload_id=7, output_path=pose, receptor_path=data / "1s63_zn_site.pdbqt")
    runtime = AMDockVSRuntime()
    window = None
    try:
        runtime.create_project(name="diagram", folder=tmp_path / "project")
        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        results = window.open_or_focus_view(COMPLEXES_VIEW_ID)
        wait(lambda: not _ACTIVE_TASKS)
        page = results.current_page()
        page._selected_hit = hit
        page._load_interactions(hit)
        dock = window.diagram_dock
        # Completion must use this selected pose even if the hidden dock has
        # not followed its selection yet.
        assert dock._hit is None
        assert not dock.isVisible()
        page.build_diagram_btn.click()
        wait(lambda: page.build_diagram_btn.isEnabled())
        assert diagram_path_for(pose, 2, suffix=".json").exists()
        assert dock.isVisible() and window.dock_manager.buttons["diagram"].isChecked()
        assert dock._hit is hit and dock._pose_rank == 2
        assert dock._diagram is not None
        assert dock.view._pages.currentWidget() is dock.view._view
        assert "pose 2" in dock.status.text()

        # An error, a different selected pose, or leaving Results must not open
        # a diagram for the old request over the user's current work.
        window.dock_manager.toggle("diagram", False)
        page._on_diagram_built(1, "Build failed")
        assert not dock.isVisible() and page.interactions_status.text() == "Build failed"
        page._on_diagram_built(99, "")
        assert not dock.isVisible()
        window.open_or_focus_view(LIGANDS_VIEW_ID)
        page._on_diagram_built(1, "")
        assert not dock.isVisible()
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
