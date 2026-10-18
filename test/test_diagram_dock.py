"""The 2D dock draws the selected pose even when no diagram was saved for it: it builds one off
the GUI thread, saves it beside the pose and shows it, instead of asking for a button press."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_a_pose_without_a_saved_diagram_gets_one_drawn(tmp_path):
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
    assert dock.stack.currentWidget() is dock.placeholder and "Drawing" in dock.placeholder.text()
    deadline = time.monotonic() + 60
    while dock.view is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    assert dock.stack.currentWidget() is dock.view and "interactions" in dock.status.text()
    assert diagram_path_for(pose, 1, suffix=".json").exists()  # the next click is a JSON read
    window.close()
