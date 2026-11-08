"""Opt-in real docking UI walkthrough; keeps GUI and worker lifetimes in a separate process."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("AMDOCK_TEST_TUTORIAL_E2E") != "1",
                    reason="Set AMDOCK_TEST_TUTORIAL_E2E=1 to run the real Vina walkthrough")
@pytest.mark.parametrize("prepare_before_box", [False, True])
def test_simple_docking_tutorial(tmp_path, prepare_before_box):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    args = ["--prepare-before-box"] if prepare_before_box else []
    result = subprocess.run([sys.executable, str(script), str(tmp_path), *args],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert (tmp_path / "completed.png").is_file()
    steps = range(1, 9)
    assert all((tmp_path / f"step-{index}.png").is_file() for index in steps)
    assert all((tmp_path / f"import-{role}-empty.png").is_file() for role in ("receptor", "ligand"))
    assert all((tmp_path / f"file-picker-{role}.png").is_file() for role in ("receptor", "ligand"))
    assert (tmp_path / "box-values.png").is_file()
    assert (tmp_path / "project-empty.png").is_file()
    assert (tmp_path / "project-ready.png").is_file()
    assert (tmp_path / "final-card-ready.png").is_file()
    if not prepare_before_box:
        assert (tmp_path / "final-card-before-preparation.png").is_file()
    if prepare_before_box:
        assert (tmp_path / "prepared-before-box.png").is_file()


def test_cancel_during_receptor_preview_does_not_abort_qt(tmp_path):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    result = subprocess.run([sys.executable, str(script), str(tmp_path), "--cancel-during-preview"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.environ.get("AMDOCK_TEST_TUTORIAL_NATIVE") != "1",
                    reason="Set AMDOCK_TEST_TUTORIAL_NATIVE=1 with a real desktop/OpenGL display")
@pytest.mark.parametrize("prepare_before_box", [False, True])
def test_simple_docking_tutorial_native_pymol(tmp_path, prepare_before_box):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    args = ["--prepare-before-box"] if prepare_before_box else []
    result = subprocess.run([sys.executable, str(script), str(tmp_path), "--native", *args],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert (tmp_path / "graphics.json").is_file()
    graphics = json.loads((tmp_path / "graphics.json").read_text())
    assert graphics["platform"] not in {"offscreen", "minimal"}
    assert [check["stage"] for check in graphics["checks"]] == ["docked", "detached", "reattached", "completed"]
    assert graphics["pose_atoms"] > 0 and isinstance(graphics["score"], float)
    assert all((tmp_path / f"pymol-{stage}.png").is_file()
               for stage in ("docked", "detached", "reattached", "completed"))


@pytest.mark.skipif(os.environ.get("AMDOCK_TEST_TUTORIAL_NATIVE") != "1",
                    reason="Set AMDOCK_TEST_TUTORIAL_NATIVE=1 with a real desktop/OpenGL display")
@pytest.mark.parametrize("program_navigation", [False, True])
def test_tutorial_box_controls_are_reachable_with_the_mouse(tmp_path, program_navigation):
    script = Path(__file__).with_name("simple_docking_walkthrough.py")
    args = ["--program-navigation"] if program_navigation else []
    result = subprocess.run([sys.executable, str(script), str(tmp_path), "--native", "--strict-input", *args],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert (tmp_path / "step-7.png").is_file()  # explicit box-definition step
    assert (tmp_path / "completed.png").is_file()
    assert (tmp_path / "final-card-before-preparation.png").is_file()
    assert (tmp_path / "final-card-ready.png").is_file()
    if program_navigation:
        assert (tmp_path / "program-navigation-recovery.png").is_file()
