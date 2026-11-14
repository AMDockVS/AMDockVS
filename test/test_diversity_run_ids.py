"""A clustering run id names one result: a second job under it must not touch the first.

It used to be submitted anyway: its worker rewrote the run's graph sidecar before the sink hit the
UNIQUE constraint, so the failed job left the DB saying 103 clusters and the graph showing 3.
"""
import pytest

from amdockvs.runtime import AMDockVSRuntime


def _completed(runtime, job_id):
    status, = runtime.wait_for_jobs([job_id], timeout_s=120).values()
    assert status.status == "completed", status.error


def test_a_run_id_in_use_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="run_ids", folder=tmp_path / "project")
        smiles = tmp_path / "ligands.smi"
        smiles.write_text("CCO a\nCCN b\nCCCC c\nc1ccccc1 d\nCC(=O)O e\n", encoding="utf-8")
        runtime.wait_for_jobs(runtime.loader.load_ligands([smiles], executor_name="thread"), timeout_s=120)
        _completed(runtime, runtime.diversity.cluster_job(
            method="bitbirch", threshold=0.35, cluster_run_id="collision", num_cpus=1, executor_name="thread"))
        sidecar = runtime.diversity._project_root() / "clustering_runs" / "collision.parquet"
        before = (runtime.diversity.get_run("collision"), sidecar.read_bytes())

        submitted = []
        monkeypatch.setattr(runtime, "submit_job", lambda *a, **k: submitted.append(a) or "job")
        with pytest.raises(ValueError, match="collision.*already"):
            runtime.diversity.cluster_job(method="bitbirch", threshold=0.1, cluster_run_id="collision", num_cpus=1)

        assert submitted == []
        assert (runtime.diversity.get_run("collision"), sidecar.read_bytes()) == before
        # A blank id still gets a fresh one, as before.
        runtime.diversity.cluster_job(method="bitbirch", threshold=0.1, num_cpus=1)
        assert len(submitted) == 1
    finally:
        runtime.shutdown()
