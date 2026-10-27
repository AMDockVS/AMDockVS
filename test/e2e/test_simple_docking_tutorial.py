"""Opt-in real docking UI walkthrough; keeps GUI and worker lifetimes in a separate process."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("AMDOCK_TEST_TUTORIAL_E2E") != "1",
                    reason="Set AMDOCK_TEST_TUTORIAL_E2E=1 to run the real Vina walkthrough")
def test_simple_docking_tutorial(tmp_path):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    result = subprocess.run([sys.executable, str(script), str(tmp_path)],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "completed.png").is_file()
    assert all((tmp_path / f"step-{index}.png").is_file() for index in range(1, 9))
    assert all((tmp_path / f"import-{role}-empty.png").is_file() for role in ("receptor", "ligand"))
    assert all((tmp_path / f"file-picker-{role}.png").is_file() for role in ("receptor", "ligand"))
    assert (tmp_path / "box-values.png").is_file()
    assert (tmp_path / "project-empty.png").is_file()
    assert (tmp_path / "project-ready.png").is_file()


def test_cancel_during_receptor_preview_does_not_abort_qt(tmp_path):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    result = subprocess.run([sys.executable, str(script), str(tmp_path), "--cancel-during-preview"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.environ.get("AMDOCK_TEST_TUTORIAL_NATIVE") != "1",
                    reason="Set AMDOCK_TEST_TUTORIAL_NATIVE=1 with a real desktop/OpenGL display")
def test_simple_docking_tutorial_native_pymol(tmp_path):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    result = subprocess.run([sys.executable, str(script), str(tmp_path), "--native"],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "graphics.json").is_file()
    graphics = json.loads((tmp_path / "graphics.json").read_text())
    assert graphics["platform"] not in {"offscreen", "minimal"}
    assert [check["stage"] for check in graphics["checks"]] == ["docked", "detached", "reattached", "completed"]
    assert graphics["pose_atoms"] > 0 and isinstance(graphics["score"], float)
    assert all((tmp_path / f"pymol-{stage}.png").is_file()
               for stage in ("docked", "detached", "reattached", "completed"))
