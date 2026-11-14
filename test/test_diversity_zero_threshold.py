"""A threshold of 0 is a valid setting (both the GUI spin box and the job params allow it), not "unset".

`x or default` read 0.0 as missing: the GUI submitted 0.35 and the worker ran 0.65, so the run
disagreed with its one-cluster preview and with the threshold it recorded.
"""
from types import SimpleNamespace
from unittest.mock import Mock

from amdockvs.runtime import AMDockVSRuntime  # noqa: F401 - pydantic before PySide6
from PySide6.QtWidgets import QApplication


def _completed(runtime, job_id):
    status, = runtime.wait_for_jobs([job_id], timeout_s=120).values()
    assert status.status == "completed", status.error


def test_a_zero_threshold_run_matches_its_one_cluster_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="zero_threshold", folder=tmp_path / "project")
        smiles = tmp_path / "ligands.smi"
        smiles.write_text("CCO a\nCCN b\nCCCC c\nc1ccccc1 d\nCC(=O)O e\n", encoding="utf-8")
        runtime.wait_for_jobs(runtime.loader.load_ligands([smiles], executor_name="thread"), timeout_s=120)

        preview = runtime.diversity.analyze(method="bitbirch", threshold=0.0)
        assert len(set(preview.labels)) == 1  # the preview: one cluster

        _completed(runtime, runtime.diversity.cluster_job(
            method="bitbirch", threshold=0.0, cluster_run_id="zero_threshold", num_cpus=1, executor_name="thread",
        ))
        run = runtime.diversity.get_run("zero_threshold")
        assert len(run) == 5
        assert len({row["cluster_id"] for row in run}) == 1
    finally:
        runtime.shutdown()


def test_the_tool_submits_the_zero_threshold_its_control_shows(monkeypatch):
    from amdockvs.ui.tools.molecules import diversity
    from amdockvs.ui.tools.molecules.diversity import DiversitySelectionWidget

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(diversity.QInputDialog, "getInt", lambda *a, **k: (1, True))
    cluster_job = Mock(return_value="job")
    tool = SimpleNamespace(
        run_button=Mock(), cluster_button=Mock(), stats_label=Mock(), _running=True,
        set_combo=SimpleNamespace(currentText=lambda: "All ligands", currentData=lambda: None),
        runtime=SimpleNamespace(diversity=SimpleNamespace(cluster_job=cluster_job)),
        _connect_job_signal=lambda: None,
    )
    scope = {"with_activity": "", "molecule_set": None, "molecule_filters": {}, "fp_radius": 2, "fp_nbits": 2048}

    DiversitySelectionWidget._confirm_and_submit(tool, 5, scope, {"method": "bitbirch", "threshold": 0.0})

    assert cluster_job.call_args.kwargs["threshold"] == 0.0
    assert tool._pending_cluster_job["threshold"] == 0.0  # what the finished run is registered with
