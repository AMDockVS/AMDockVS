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
