"""The 2D dock only shows saved diagrams: a click computes nothing. Once Build diagram (or a
job) saved one beside the pose, reload() draws it."""
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
    assert dock.stack.currentWidget() is dock.placeholder and "Build diagram" in dock.placeholder.text()
    assert not diagram_path_for(pose, 1, suffix=".json").exists()  # nothing computed by the click

    from ms_contactmap import solve_layout

    from amdockvs.docking.results.diagram import build_pose_diagram, save_pose_diagram

    diagram = build_pose_diagram(pose_path=pose, receptor_path=hit.receptor_path, name="lig")
    save_pose_diagram(pose, 1, diagram, solve_layout(diagram))  # what Build diagram does
    dock.reload()
    assert dock.stack.currentWidget() is dock.view and "interactions" in dock.status.text()
    window.close()
