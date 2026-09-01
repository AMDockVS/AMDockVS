import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
sys.path.insert(0, "//src")

from amdockvs import AMDockVSRuntime
from screening_benchmark import BenchmarkConfig, run_screening_benchmark


def _patch_fake_home(monkeypatch, fake_home: Path):
    monkeypatch.setenv("HOME", str(fake_home))


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_throughput_profile_handles_heavier_screening_volume(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    pytest.importorskip("meeko")
    pytest.importorskip("vina")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        # The old docking.screen(...) pipeline moved out of DockingAPI; run_screening_benchmark
        # is the current end-to-end screening runner (it provisions its own project + inputs).
        # This exercises a heavier volume (240 ligands x 2 receptors = 480 dockings) under the
        # throughput batch policy.
        result = run_screening_benchmark(
            runtime,
            config=BenchmarkConfig(
                ligand_count=240,
                receptor_count=2,
                ligand_file_count=2,
                policy="throughput",
                timeout_s=240.0,
                poll_s=0.05,
            ),
            workdir=tmp_path / "throughput_run",
        )

        assert result["policy"]["name"] == "throughput"
        assert result["counts"]["ligands"] == 240
        assert result["counts"]["receptors"] == 2
        assert result["counts"]["descriptors"] == 240
        assert result["counts"]["results"] == 480
        assert result["jobs"]["descriptor_status"] == "completed"
        assert result["jobs"]["docking_status"] == "completed"
    finally:
        runtime.shutdown()
