"""A saved clustering run keeps the PCA variance its graph was drawn with, or says it has none.

The worker computed it and threw it away; registering the run then stored [0.0, 0.0], and the
saved plot labelled both axes 0.0% — a false statistic that survived reopening the project.
"""
import pytest

from amdockvs.runtime import AMDockVSRuntime


def test_a_saved_run_reports_the_variance_its_graph_was_projected_with(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="saved_variance", folder=tmp_path / "project")
        smiles = tmp_path / "ligands.smi"
        smiles.write_text("CCO a\nCCN b\nCCCC c\nc1ccccc1 d\nCC(=O)O e\nc1ccncc1 f\n", encoding="utf-8")
        runtime.wait_for_jobs(runtime.loader.load_ligands([smiles], executor_name="thread"), timeout_s=120)
        status, = runtime.wait_for_jobs([runtime.diversity.cluster_job(
            method="bitbirch", threshold=0.35, cluster_run_id="variance", num_cpus=1, executor_name="thread",
        )], timeout_s=120).values()
        assert status.status == "completed", status.error

        runtime.diversity.register_run_from_sidecar("variance", method="bitbirch", threshold=0.35, scope_label="all")
        evr = runtime.diversity.load_clustering_result("variance")["evr"]
        assert len(evr) == 2 and all(0.0 < value < 1.0 for value in evr)
    finally:
        runtime.shutdown()


def test_a_run_saved_without_its_variance_says_so_instead_of_zero(tmp_path, monkeypatch):
    from sqlmodel import select

    from amdockvs.models import ClusteringRun

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="old_run", folder=tmp_path / "project")
        # What registering wrote before the variance was kept: [0.0, 0.0].
        with runtime.workspace.project_db.get_session() as session:
            session.add(ClusteringRun(run_id="old", method="bitbirch", threshold=0.35, scope_label="all",
                                      evr=[0.0, 0.0]))
            session.commit()
        assert runtime.diversity.load_clustering_result("old")["evr"] == []
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("evr, bottom, in_title", [
    ([0.0395, 0.0381], "PC1 (4.0%)", "7.8% variance"),
    ([], "PC1", "variance not recorded"),
])
def test_the_universe_plot_labels_known_and_unknown_variance(monkeypatch, evr, bottom, in_title):
    from ms_components.ms_dockwidget.widget import DockManager
    from PySide6.QtWidgets import QApplication, QMainWindow

    from amdockvs.ui.tools.qsar.chart import QSARChartDockWidget

    QApplication.instance() or QApplication([])
    window = QMainWindow()
    chart = QSARChartDockWidget("Chart", DockManager(window), window)
    chart.show_universe([(0.0, 0.0), (1.0, 1.0)], [{"points": [(0.5, 0.5)], "ids": [1], "color": (1, 2, 3), "label": "r"}], evr)
    plot = chart.universe_view.getPlotItem()
    assert plot.getAxis("bottom").labelText == bottom
    assert in_title in plot.titleLabel.text
