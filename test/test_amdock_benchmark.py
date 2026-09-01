import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, "//src")

from amdockvs import AMDockVSRuntime
from screening_benchmark import BenchmarkConfig, run_screening_benchmark
from screening_benchmark import list_screening_batch_policies


def _patch_fake_home(monkeypatch, fake_home: Path):
    monkeypatch.setenv("HOME", str(fake_home))


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_runtime_exposes_named_screening_batch_policies(tmp_path, monkeypatch):
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        policies = list_screening_batch_policies()

        assert [policy.name for policy in policies] == ["latency", "balanced", "throughput"]
        assert policies[0].docking_batch_size < policies[-1].docking_batch_size
    finally:
        runtime.shutdown()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_screening_benchmark_reports_counts_and_throughput(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    pytest.importorskip("meeko")
    pytest.importorskip("vina")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        result = run_screening_benchmark(
            runtime,
            config=BenchmarkConfig(
                ligand_count=24,
                receptor_count=1,
                ligand_file_count=2,
                policy="balanced",
                timeout_s=120.0,
                poll_s=0.05,
            ),
            workdir=tmp_path / "benchmark_run",
        )

        assert result["policy"]["name"] == "balanced"
        assert result["counts"]["ligands"] == 24
        assert result["counts"]["receptors"] == 1
        assert result["counts"]["descriptors"] == 24
        assert result["counts"]["results"] == 24
        assert result["jobs"]["descriptor_status"] == "completed"
        assert result["jobs"]["docking_status"] == "completed"
        assert result["durations_s"]["total"] > 0.0
        assert result["throughput"]["ligands_per_s"] > 0.0
        assert result["throughput"]["results_per_s"] > 0.0
    finally:
        runtime.shutdown()
