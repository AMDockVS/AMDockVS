"""The external-tool registry: status, install plan, removal."""

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_every_managed_tool_reports_a_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    from amdockvs.external_tools import MANAGED_TOOLS, tool_statuses

    statuses = tool_statuses(SimpleNamespace())

    assert [status.tool_id for status in statuses] == [tool.tool_id for tool in MANAGED_TOOLS]
    # Nothing lives under a fresh tools home (a tool already on PATH may still report ready).
    assert all(status.location is None and status.size_bytes == 0 for status in statuses)
    assert not next(status for status in statuses if status.tool_id == "p2rank").installed


def test_p2rank_plan_installs_java_only_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    import amdockvs.external_tools as external_tools

    monkeypatch.setattr(external_tools, "java_major_version", lambda _command=None: None)
    without_java = external_tools.install_steps(SimpleNamespace(), "p2rank")
    monkeypatch.setattr(external_tools, "java_major_version", lambda _command=None: 21)
    with_java = external_tools.install_steps(SimpleNamespace(), "p2rank")

    assert len(without_java) == len(with_java) + 1
    assert "jdk4py~=21.0" in (without_java[0].argv or [])
    assert with_java[0].call is not None  # only the download/extract step is left


def test_uninstall_removes_the_directory_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    from amdockvs.external_tools import uninstall_tool
    from amdockvs.pockets.p2rank import p2rank_home

    home = Path(p2rank_home())
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "p2rank.jar").write_text("jar")

    assert "Removed" in uninstall_tool(SimpleNamespace(), "p2rank")
    assert not home.exists()
    assert "not installed" in uninstall_tool(SimpleNamespace(), "p2rank")


def test_unknown_tool_is_rejected():
    from amdockvs.external_tools import get_tool

    with pytest.raises(KeyError):
        get_tool("autodock5")
