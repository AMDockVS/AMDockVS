"""A Diversity run read as a catalog: clusters on top, their ligands below, explicit actions.

What is worth a test is the contract the redesign rests on: the tool's scope is visible on the
Ligands table, a run changes nothing by itself, and its ligands become the shared selection (or
get excluded) only when asked.
"""
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("rdkit")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amdockvs.models.clustering import ClusteringResult
from amdockvs.models.molecules import MoleculeRecord
from amdockvs.runtime import AMDockVSRuntime
from amdockvs.ui.main_window import AMDockVSMainWindow
from amdockvs.ui.view_ids import CLUSTERS_VIEW_ID, LIGANDS_VIEW_ID, SELECTION_VIEW_ID


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
        with runtime.workspace.project_db.get_session() as session:
            ids = sorted(session.exec(select(MoleculeRecord.id)).all())
        assert len(ids) == 4

        # Two clusters: {ids[0] (rep), ids[1]} and {ids[2] (rep), ids[3]}.
        layout = [(ids[0], 0, True), (ids[1], 0, False), (ids[2], 1, True), (ids[3], 1, False)]
        run_id = runtime.diversity.save_clustering_result(
            points=[(float(i), 0.0, mid, cid, rep) for i, (mid, cid, rep) in enumerate(layout)],
            cluster_stats=[{"cluster_id": 0, "size": 2, "tightness": 0.0}, {"cluster_id": 1, "size": 2, "tightness": 0.0}],
            evr=[0.5, 0.2], method="bitbirch", threshold=0.35, scope_label="All ligands",
        )
        with runtime.workspace.project_db.get_session() as session:
            session.add_all(ClusteringResult(**row) for row in ClusteringResult.build_rows(
                run_id, [{"molecule_id": m, "cluster_id": c, "is_centroid": r} for m, c, r in layout]))
            session.commit()

        runtime.qsar.set_activity(ligand_id=ids[0], endpoint="pIC50", value=6.0)
        window = AMDockVSMainWindow(runtime=runtime)
        window.resize(1440, 900)
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
        # Rows picked in the tool's table are what it works on: the choice follows them...
        window.selection.set("ligand", ids[:3])
        assert tool.set_combo.currentText() == "Current selection"
        assert tool._scope_params()["molecule_filters"] == {"id__in": ids[:3]}
        # ...and any other choice is a set of its own, whatever is selected.
        tool.set_combo.setCurrentIndex(tool.set_combo.findText("Reference"))
        assert tool._scope_params()["molecule_filters"] == {"usage_class": "reference"}
        assert "id" not in _filters(ligands) and window.selection.get("ligand") == ids[:3]
        window.selection.set("ligand", None)
        tool.set_combo.setCurrentIndex(tool.set_combo.findText("Current selection"))
        monkeypatch.setattr("amdockvs.ui.tools.molecules.diversity.QMessageBox.information", lambda *a, **k: None)
        assert tool._scope_params() is None and "nothing selected" in tool.scope_label.text()
        tool.set_combo.setCurrentIndex(0)
        # The scope can be narrowed to the ligands measured for an activity.
        assert tool._scope_params()["with_activity"] == ""
        preview_errors = []
        monkeypatch.setattr("amdockvs.ui.tools.molecules.diversity.QMessageBox.critical",
                            lambda *a, **k: preview_errors.append(a[2]))

        def preview():
            tool.run_button.click()
            _wait(app, lambda: not tool._running)
            assert not preview_errors
            return set(tool._last_result["molecule_ids"])

        assert preview() == set(ids)
        chart = window.qsar_chart_dock
        preview_ids = set(tool._seen_ids)
        preview_count = len(tool._selections)
        tool.size_dist_button.click()
        assert chart.stack.currentWidget() is chart.chart_plot
        tool.plot_button.click()
        assert chart.stack.currentWidget() is chart.universe_view
        assert tool._seen_ids == preview_ids and len(tool._selections) == preview_count
        # A scope smaller than a sample is exhausted after one press. Preview must
        # still restore the scatter, and changed settings must be evaluated anew.
        tool.size_dist_button.click()
        assert preview() == set(ids)
        assert chart.stack.currentWidget() is chart.universe_view
        assert "Whole scope already previewed" in tool.stats_label.text()
        assert tool._seen_ids == preview_ids and len(tool._selections) == preview_count
        from PySide6.QtWidgets import QDoubleSpinBox

        threshold = tool.method_stack.currentWidget().findChild(QDoubleSpinBox)
        threshold.setValue(0.65)
        assert preview() == set(ids)
        assert tool._last_result["threshold"] == 0.65
        assert len(tool._selections) == 1
        tool.measured_combo.setCurrentIndex(tool.measured_combo.findData("pIC50"))
        assert tool._scope_params()["with_activity"] == "pIC50" and "pIC50" in tool.scope_label.text()
        assert runtime.diversity.scope_count(with_activity="pIC50") == 1
        assert preview() == {ids[0]}
        # A singleton has no grey background points, but its representative still
        # counts as a completed preview and must be restored on the next press.
        assert not tool._pool_points
        tool.size_dist_button.click()
        assert preview() == {ids[0]}
        assert chart.stack.currentWidget() is chart.universe_view
        assert "Whole scope already previewed" in tool.stats_label.text()
        assert window.selection.get("ligand") is None
        tool.measured_combo.setCurrentIndex(0)

        # Clusters: the run, its two clusters under a "Representatives" row, members below.
        tool._show_clusters(run_id)
        view = window.central_widget.open_view(CLUSTERS_VIEW_ID)
        _wait(app, lambda: view.table.rowCount() == 3)
        assert window.aux.occupant is not None and view.members.parent() is not None  # hosted in the aux zone
        _wait(app, lambda: view.members.table._table.model().rowCount() == 2)

        # The tool and result controls must leave room for the plot and movable dividers.
        assert tool.minimumSizeHint().width() < 480
        assert view.minimumSizeHint().width() < 540
        assert window.qsar_chart_dock.width() >= 300
        window.resize(1440, 900)
        window.resizeDocks([window.tools_dock], [360], Qt.Horizontal)
        window.resizeDocks([window.qsar_chart_dock], [420], Qt.Horizontal)
        app.processEvents()
        assert window.width() <= 1440
        assert window.tools_dock.width() < 480
        assert window.qsar_chart_dock.width() >= 350
        chart_width = window.qsar_chart_dock.width()
        window.charts.sync_to_active_view(LIGANDS_VIEW_ID)
        view.restore_plot()
        app.processEvents()
        assert window.qsar_chart_dock.width() == chart_width
        splitter = window.central_widget.content_splitter
        before = splitter.sizes()
        splitter.moveSplitter(splitter.height() // 2, 1)
        app.processEvents()
        assert abs(splitter.sizes()[0] - before[0]) > 30

        # Returning from the histogram redraws the saved plot; no clustering or sampling.
        from unittest.mock import Mock

        with monkeypatch.context() as guard:
            guard.setattr(runtime.diversity, "cluster_job", Mock(side_effect=AssertionError("reclustered")))
            guard.setattr(runtime.diversity, "load_universe", Mock(side_effect=AssertionError("resampled")))
            view.table.selectRow(1)
            picked_cluster = view._picked_cluster()
            view.sizes_button.click()
            assert chart.stack.currentWidget() is chart.chart_plot
            view.plot_button.click()
            assert chart.stack.currentWidget() is chart.universe_view
            assert view._run_id == run_id and view._picked_cluster() == picked_cluster
            assert window.selection.get("ligand") is None
        view.table.selectRow(0)

        # What represents each cluster is chosen here, on the stored run: nothing is clustered again.
        errors: list[str] = []
        monkeypatch.setattr("amdockvs.ui.tools.molecules.clusters.QMessageBox.critical", lambda *a, **k: errors.append(a[2]))
        view.mode_combo.setCurrentIndex(view.mode_combo.findData("extremes"))
        assert not view.amount_spin.isEnabled() and view.rank_combo.findData("pIC50") >= 0
        view.apply_button.click()  # this run was saved without member rankings: it says so
        _wait(app, lambda: bool(errors) and "cluster again" in errors[0])
        sidecar = runtime.diversity._project_root() / "clustering_runs" / f"{run_id}.parquet"
        pd.read_parquet(sidecar).assign(similarity=[1.0, 0.5, 1.0, 0.5], centrality=float("nan")).to_parquet(sidecar)
        view.apply_button.click()  # lowest and highest of two members: both
        _wait(app, lambda: view.table.item(0, 1).text() == "4")
        # Apply replaces the data of the already-selected Representatives row.
        # Both displays must refresh even though the row selection did not change.
        _wait(app, lambda: view.members.table._table.model().rowCount() == 4)
        assert "4 representatives" in chart.universe_view.plotItem.titleLabel.text
        view.mode_combo.setCurrentIndex(view.mode_combo.findData("proportional"))
        assert view.amount_spin.suffix() == " %" and view.amount_spin.value() == 10
        view.mode_combo.setCurrentIndex(view.mode_combo.findData("centroid"))
        view.apply_button.click()  # ...and back to the one nearest the centroid
        _wait(app, lambda: view.table.item(0, 1).text() == "2")
        _wait(app, lambda: view.members.table._table.model().rowCount() == 2)
        assert "2 representatives" in chart.universe_view.plotItem.titleLabel.text
        assert runtime.diversity.centroid_ids(run_id) == [ids[0], ids[2]]

        # Nothing happened to the library by running; "Use as selection" is what selects.
        assert window.selection.get("ligand") is None
        view.select_button.click()
        _wait(app, lambda: window.selection.get("ligand") == [ids[0], ids[2]])
        # The tool is on "All ligands", so the table it borrows ignores the selection — a
        # selection made here does not move its choice — until the tool gives the table back.
        assert tool.set_combo.currentIndex() == 0 and "id" not in _filters(ligands)
        window.tools.on_tool_action(SELECTION_VIEW_ID, False)
        app.processEvents()
        assert _filters(ligands)["id"] == [ids[0], ids[2]]

        view.table.selectRow(2)  # a cluster: its members, representative or not
        picked = view._picked_cluster()
        view.select_button.click()
        _wait(app, lambda: window.selection.get("ligand") == sorted(m for m, c, _ in layout if c == picked))

        # ...and "Exclude non-representatives" is what excludes.
        view.exclude_button.click()
        _wait(app, lambda: "Excluded 2" in view.status.text())
        with runtime.workspace.project_db.get_session() as session:
            excluded = sorted(session.exec(select(MoleculeRecord.id).where(MoleculeRecord.excluded == True)).all())  # noqa: E712
        assert excluded == [ids[1], ids[3]]
    finally:
        if window is not None:
            window.close()
        runtime.shutdown()
