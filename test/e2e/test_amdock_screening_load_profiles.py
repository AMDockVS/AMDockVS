import shutil
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
    pytest.importorskip("ms_adtools")
    from amdockvs.core.constants import vina_command
    if not shutil.which(vina_command()):
        pytest.skip("the vina program is not installed")
    _patch_fake_home(monkeypatch, tmp_path)

    runtime = AMDockVSRuntime()
    try:
        # The old docking.screen(...) pipeline moved out of DockingAPI; run_screening_benchmark
        # is the current end-to-end screening runner (it provisions its own project + inputs).
        # The throughput batch policy over 2 receptors (48 ligands x 2 = 96 dockings). ponytail:
        # exhaustiveness 1 and a small volume, this checks the path, not the speed; the old
        # 480 dockings at exhaustiveness 8 took ~540 s of the suite.
        result = run_screening_benchmark(
            runtime,
            config=BenchmarkConfig(
                ligand_count=48,
                receptor_count=2,
                ligand_file_count=2,
                policy="throughput",
                exhaustiveness=1,
                timeout_s=900.0,
                poll_s=0.05,
            ),
            workdir=tmp_path / "throughput_run",
        )

        assert result["policy"]["name"] == "throughput"
        assert result["counts"]["ligands"] == 48
        assert result["counts"]["receptors"] == 2
        assert result["counts"]["descriptors"] == 48
        assert result["counts"]["results"] == 96
        assert result["jobs"]["descriptor_status"] == "completed"
        assert result["jobs"]["docking_status"] == "completed"
    finally:
        runtime.shutdown()
