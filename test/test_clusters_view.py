"""A Diversity run read as a catalog: clusters on top, their ligands below, explicit actions.

What is worth a test is the contract the redesign rests on: the tool's scope is visible on the
Ligands table, a run changes nothing by itself, and its ligands become the shared selection (or
get excluded) only when asked.
"""
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("rdkit")
from PySide6.QtWidgets import QApplication
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.models.clustering import ClusteringResult
from amdockvs.models.molecules import MoleculeRecord
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.ui.catalog import LIGANDS_VIEW_ID
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.tools.molecules.clusters import CLUSTERS_VIEW_ID
from amdockvs.ui.tools.molecules.diversity import SELECTION_VIEW_ID


def _wait(app, done, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not done() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert done()


def _filters(table) -> dict:
    return {f.field: f.value for f in table.table._builder.active_filters}


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_run_is_read_in_clusters_and_changes_nothing_until_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AMDOCK_DISABLE_PYMOL", "1")
    monkeypatch.setattr("amdockvs.ui.tools.molecules.clusters.QMessageBox.question",
                        lambda *a, **k: __import__("PySide6.QtWidgets").QtWidgets.QMessageBox.Yes)
    app = QApplication.instance() or QApplication(["amdockvs-ui"])
    window = None
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="clusters", folder=tmp_path / "clusters", description="clusters view")
        smiles = tmp_path / "ligands.smi"
        smiles.write_text("CCO a\nCCN b\nCCCC c\nc1ccccc1 d\n", encoding="utf-8")
        runtime.wait_for_jobs(runtime.loader.load_ligands([smiles], batch_size=4, executor_name="thread"), timeout_s=120)
        with runtime.molsuite.project_db.get_session() as session:
            ids = sorted(session.exec(select(MoleculeRecord.id)).all())
        assert len(ids) == 4

        # Two clusters: {ids[0] (rep), ids[1]} and {ids[2] (rep), ids[3]}.
        layout = [(ids[0], 0, True), (ids[1], 0, False), (ids[2], 1, True), (ids[3], 1, False)]
        run_id = runtime.diversity.save_clustering_result(
            points=[(float(i), 0.0, mid, cid, rep) for i, (mid, cid, rep) in enumerate(layout)],
            cluster_stats=[{"cluster_id": 0, "size": 2, "tightness": 0.0}, {"cluster_id": 1, "size": 2, "tightness": 0.0}],
            evr=[0.5, 0.2], method="bitbirch", threshold=0.35, scope_label="All ligands",
        )
        with runtime.molsuite.project_db.get_session() as session:
            session.add_all(ClusteringResult(**row) for row in ClusteringResult.build_rows(
                run_id, [{"molecule_id": m, "cluster_id": c, "is_centroid": r} for m, c, r in layout]))
            session.commit()

        window = AMDockVSMainWindow(runtime=runtime)
        window.show()
        app.processEvents()

        # The tool shows its scope on the Ligands table, and gives the table back when it hides.
        tool = window.tools.open_tool(SELECTION_VIEW_ID)
        app.processEvents()
        ligands = window.central_widget.open_view(LIGANDS_VIEW_ID)
        assert window.central_widget.current_view_id() == LIGANDS_VIEW_ID
        _wait(app, lambda: "the 4 ligand(s)" in tool.scope_label.text())  # the scope, in words
        tool.set_combo.setCurrentIndex(tool.set_combo.findText("Reference"))
        assert _filters(ligands)["usage_class"] == "reference"
        assert tool._scope_params()["molecule_filters"] == {"usage_class": "reference"}
        tool.set_combo.setCurrentIndex(0)
        window.selection.set("ligand", ids[:3])  # a "Select" in Ligands narrows the run
        assert tool._scope_params()["molecule_filters"] == {"id__in": ids[:3]}
        window.selection.set("ligand", None)

        # Clusters: the run, its two clusters under a "Representatives" row, members below.
        tool._show_clusters(run_id)
        view = window.central_widget.open_view(CLUSTERS_VIEW_ID)
        _wait(app, lambda: view.table.rowCount() == 3)
        assert window.aux.occupant is not None and view.members.parent() is not None  # hosted in the aux zone
        _wait(app, lambda: view.members.table._table.model().rowCount() == 2)

        # Nothing happened to the library by running; "Use as selection" is what selects.
        assert window.selection.get("ligand") is None
        view.select_button.click()
        _wait(app, lambda: window.selection.get("ligand") == [ids[0], ids[2]])
        assert _filters(ligands)["id"] == [ids[0], ids[2]]

        view.table.selectRow(2)  # a cluster: its members, representative or not
        picked = view._picked_cluster()
        view.select_button.click()
        _wait(app, lambda: window.selection.get("ligand") == sorted(m for m, c, _ in layout if c == picked))

        # ...and "Exclude non-representatives" is what excludes.
        view.exclude_button.click()
        _wait(app, lambda: "Excluded 2" in view.status.text())
        with runtime.molsuite.project_db.get_session() as session:
            excluded = sorted(session.exec(select(MoleculeRecord.id).where(MoleculeRecord.excluded == True)).all())  # noqa: E712
        assert excluded == [ids[1], ids[3]]
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
