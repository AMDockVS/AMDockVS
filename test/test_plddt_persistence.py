"""Saved ESMFold confidence remains available after preparation and a new runtime."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from amdockvs.runtime import AMDockVSRuntime  # pydantic before PySide6

import pymol2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from amdockvs.core.payloads import read_structure
from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.ui.catalog.details import CatalogDetailsView
from amdockvs.ui.viewers.molecular_viewer import MolecularViewerController
from payload_seed import seed_payload
from test_catalog_structure_refresh import PDB


@pytest.mark.parametrize("prepared_source", [None, "fixed", "protonated", "minimized"])
def test_reopened_prediction_keeps_plddt_and_colors_the_saved_prediction(tmp_path, monkeypatch, prepared_source):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    runtime = AMDockVSRuntime()
    project = runtime.create_project(name="saved_prediction", folder=tmp_path / "project")
    store = runtime.workspace.project_db
    older_prediction = seed_payload(store, PDB.replace("90.00", "20.00"), "pdb")
    predicted = seed_payload(store, PDB, "pdb")
    prepared = seed_payload(store, PDB.replace("90.00", " 0.00"), "pdb") if prepared_source else None
    metrics = seed_payload(store, '{"plddt_mean": 90, "ptm": 0.8, "pae": [[0]]}', "json")
    with store.get_session() as db:
        molecule = MoleculeRecord(name="prediction", molecule_type="protein", is_receptor=True,
                                  stored_payload_id=predicted, current_payload_id=prepared, has_3d=True)
        db.add(molecule)
        db.commit()
        db.refresh(molecule)
        molecule_id = molecule.id
        db.add(MoleculeModel(molecule_id=molecule_id, payload_id=older_prediction, source="esmfold"))
        db.add(MoleculeModel(molecule_id=molecule_id, payload_id=predicted, source="esmfold",
                             data_payload_id=metrics))
        if prepared_source:
            db.add(MoleculeModel(molecule_id=molecule_id, payload_id=prepared, source=prepared_source))
        experimental = MoleculeRecord(name="experimental", molecule_type="protein", is_receptor=True,
                                      stored_payload_id=predicted, has_3d=True)
        db.add(experimental)
        db.commit()
        db.refresh(experimental)
        experimental_id = experimental.id
    runtime.shutdown()

    # Use a completely new runtime: no prediction-completed signal or in-memory state.
    runtime = AMDockVSRuntime()
    view = None
    try:
        runtime.open_project(project.id)
        molecule = runtime.molecules.get(molecule_id)
        view = CatalogDetailsView(runtime=runtime)
        view.resize(950, 500)
        view.show()
        view.show_molecule(molecule)
        app.processEvents()
        assert view.show_plddt_button.isEnabled()
        assert view.plddt_legend.isVisible()
        details = runtime.molecules.details(molecule_id)
        assert details.plddt_model.payload_id == predicted
        assert details.plddt_model_data == {"plddt_mean": 90, "ptm": 0.8}
        if prepared_source:
            assert details.current_model.source == prepared_source
            assert "prediction" in view.show_plddt_button.text().lower()
        with pymol2.PyMOL() as session:
            dock = SimpleNamespace(cmd=session.cmd, show=lambda: None)
            viewer = MolecularViewerController(SimpleNamespace(runtime=runtime, pymol_dock=dock, grid_dock=None))
            viewer._read = lambda wanted, then: then({
                key: read_structure(runtime.workspace.project_db, payload) for key, payload in wanted.items()
            })
            viewer._show_molecule(molecule, source="current", object_prefix="receptor", orient=False)
            view.show_plddt_requested.connect(viewer.show_plddt)
            view.show_molecule_requested.connect(viewer.show_molecule)
            QTest.mouseClick(view.show_plddt_button, Qt.LeftButton)
            name = f"receptor_{molecule_id}"
            assert viewer._loaded[name] == predicted
            assert session.cmd.get_names("objects") == [name]
            colors = []
            session.cmd.iterate(name, "colors.append(color)", space={"colors": colors})
            assert set(colors) == {session.cmd.get_color_index("plddt_confident")}
            assert runtime.molecules.get(molecule_id).current_payload_id == prepared
            QTest.mouseClick(view.show_primary_button, Qt.LeftButton)
            assert viewer._loaded[name] == (prepared or predicted)
        if prepared_source == "minimized":
            view.grab().save("/tmp/amdockvs-reopened-plddt-details.png")
        view.show_molecule(runtime.molecules.get(experimental_id))
        assert not view.show_plddt_button.isEnabled()  # Ordinary B-factors are not confidence scores.
        assert not view.plddt_legend.isVisible()
        view.clear_details()
        assert not view.show_plddt_button.isEnabled()
    finally:
        if view is not None:
            view.close()
        runtime.shutdown()
