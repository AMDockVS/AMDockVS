"""The external-tool registry: status, install plan, removal."""

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_every_managed_tool_reports_a_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    from amdockvs.integrations.registry import MANAGED_TOOLS, tool_statuses

    statuses = tool_statuses(SimpleNamespace())

    assert [status.tool_id for status in statuses] == [tool.tool_id for tool in MANAGED_TOOLS]
    # Nothing lives under a fresh tools home (a tool already on PATH may still report ready);
    # Python packages belong to this environment, not a tools-home directory.
    assert all(status.location is None and status.size_bytes == 0
               for status in statuses if status.tool_id not in {"scikit-learn", "pdbfixer", "openmm"})
    assert not next(status for status in statuses if status.tool_id == "p2rank").installed


def test_scikit_learn_status_follows_this_environment(monkeypatch):
    import amdockvs.integrations.registry as external_tools

    assert external_tools.get_tool("scikit-learn").status(None).installed  # a test dependency here
    monkeypatch.setattr(external_tools.importlib.util, "find_spec", lambda _name: None)
    missing = external_tools.get_tool("scikit-learn").status(None)
    assert not missing.installed and missing.location is None


def test_p2rank_plan_installs_java_only_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    import amdockvs.integrations.registry as external_tools

    monkeypatch.setattr(external_tools, "java_major_version", lambda _command=None: None)
    without_java = external_tools.install_steps(SimpleNamespace(), "p2rank")
    monkeypatch.setattr(external_tools, "java_major_version", lambda _command=None: 21)
    with_java = external_tools.install_steps(SimpleNamespace(), "p2rank")

    assert len(without_java) == len(with_java) + 1
    assert "jdk4py>=21" in (without_java[0].argv or [])
    assert with_java[0].call is not None  # only the download/extract step is left


def test_uninstall_removes_the_directory_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    from amdockvs.integrations.registry import uninstall_tool
    from amdockvs.binding_sites.p2rank import p2rank_home

    home = Path(p2rank_home())
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "p2rank.jar").write_text("jar")

    assert "Removed" in uninstall_tool(SimpleNamespace(), "p2rank")
    assert not home.exists()
    assert "not installed" in uninstall_tool(SimpleNamespace(), "p2rank")


def test_uninstall_leaves_an_environment_of_the_user(monkeypatch, tmp_path):
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    from ms_env import STAMP
    from amdockvs.docking.engines.qvina import qvina_recipe
    from amdockvs.integrations.registry import uninstall_tool

    binary = Path(qvina_recipe(SimpleNamespace()).prefix, "qvina2.1")
    binary.parent.mkdir(parents=True)
    binary.write_text("put there by the user")
    assert "left as it is" in uninstall_tool(SimpleNamespace(), "qvina") and binary.exists()

    (binary.parent / STAMP).write_text("{}")  # what an install by ms_env leaves
    assert "Removed" in uninstall_tool(SimpleNamespace(), "qvina") and not binary.parent.exists()


def test_unknown_tool_is_rejected():
    from amdockvs.integrations.registry import get_tool

    with pytest.raises(KeyError):
        get_tool("autodock5")


@pytest.mark.parametrize("name", ["reduce", "pdb2pqr"])
def test_protein_protonation_is_managed_and_available_to_host_setup(monkeypatch, tmp_path, name):
    from amdockvs.integrations.registry import get_tool
    from amdockvs.integrations.hosts import HOST_PROGRAMS

    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    tool = get_tool(name)
    recipe = tool.recipe(None)
    assert tool.category == "Protein preparation" and name in HOST_PROGRAMS
    assert recipe.prefix.startswith(str(tmp_path))
    assert recipe.steps
    assert recipe.commands == ()  # A tool on PATH must not satisfy a managed install.
    if name == "reduce":
        assert "reduce=4.15" in recipe.conda
        assert "code=$?" in recipe.check[2]
    else:
        assert "pdb2pqr==3.7.1" in recipe.pip
        assert any(candidate.endswith("pdb2pqr30") for candidate in recipe.candidates)


@pytest.mark.parametrize("version,code,ready", [
    ("4.15", 0, True), ("4.15.250408", 2, True), ("4.15", 1, False),
    ("3.3.160602", 0, False), ("4.150", 0, False), ("4.16", 2, False),
])
def test_reduce_version_check_requires_the_recommended_release(tmp_path, monkeypatch, version, code, ready):
    import os
    from ms_env import READY, STAMP, locate
    from amdockvs.integrations.envs import protonation_tool_status, tool_spec

    if os.name == "nt":
        pytest.skip("Reduce's conda runtime is for Linux and macOS")
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    recipe = tool_spec("reduce").recipe(str(tmp_path))
    prefix = Path(recipe.prefix)
    command = prefix / "bin" / "reduce"
    command.parent.mkdir(parents=True)
    command.write_text(f"#!/bin/sh\necho reduce.{version}\nexit {code}\n")
    command.chmod(0o755)
    assert (locate(recipe)[0] == READY) is ready
    (prefix / STAMP).write_text("{}")
    assert protonation_tool_status(None, "reduce").installed is ready


@pytest.mark.parametrize("version", ["3.7.1", "3.6.2", "3.7.10"])
def test_pdb2pqr_version_check_requires_the_recommended_release(tmp_path, monkeypatch, version):
    import os
    import sys
    from ms_env import READY, STAMP, locate
    from amdockvs.integrations.envs import protonation_tool_status, tool_spec

    if os.name == "nt":
        pytest.skip("Shell stand-ins for the tool executables")
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    recipe = tool_spec("pdb2pqr").recipe(str(tmp_path))
    prefix = Path(recipe.prefix)
    command = prefix / "bin" / "pdb2pqr30"
    command.parent.mkdir(parents=True)
    (command.parent / "python").symlink_to(sys.executable)
    command.write_text(f"#!/bin/sh\necho pdb2pqr {version}\n")
    command.chmod(0o755)
    assert (locate(recipe)[0] == READY) is (version == "3.7.1")
    (prefix / STAMP).write_text("{}")
    assert protonation_tool_status(None, "pdb2pqr").installed is (version == "3.7.1")


@pytest.mark.parametrize("name", ["reduce", "pdb2pqr"])
def test_system_protonation_tools_do_not_hide_a_missing_managed_install(monkeypatch, tmp_path, name):
    import os
    from ms_env import MISSING, locate
    from amdockvs.integrations.envs import protonation_recipe, protonation_tool_status

    if os.name == "nt":
        pytest.skip("Shell stand-ins for the tool executables")
    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path / "managed"))
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    binary = system_bin / name
    binary.write_text("#!/bin/sh\necho " + ("reduce.4.15" if name == "reduce" else "pdb2pqr 3.7.1") + "\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(system_bin) + os.pathsep + os.environ.get("PATH", ""))
    assert locate(protonation_recipe(None, name))[0] == MISSING
    status = protonation_tool_status(None, name)
    assert not status.installed and status.command is None


@pytest.mark.parametrize("name", ["pdbfixer", "openmm"])
def test_protein_package_installs_into_the_active_python_and_verifies_imports(monkeypatch, tmp_path, name):
    import sys
    import amdockvs.integrations.registry as tools
    from ms_env import StepFailed

    files = {}
    for module in ("pdbfixer", "openmm"):
        origin = tmp_path / module / "__init__.py"
        origin.parent.mkdir()
        origin.write_text("")
        files[module] = origin
    monkeypatch.setattr(tools.importlib.util, "find_spec",
                        lambda module: SimpleNamespace(origin=str(files[module])) if module in files else None)
    imported = []
    monkeypatch.setattr(tools.importlib, "import_module", lambda module: imported.append(module))
    tool = tools.get_tool(name)
    assert tool.category == "Protein preparation" and tool.recipe is None
    assert tool.status(None).installed
    plan = tools.install_steps(None, name)
    assert plan[0].argv[:4] == [sys.executable, "-m", "pip", "install"]
    assert "openmm>=8.1" in plan[0].argv
    if name == "pdbfixer":
        assert "pdbfixer>=1.9" in plan[0].argv
    assert "ready" in plan[-1].call()
    assert "openmm.app" in imported
    files["openmm"].unlink()  # A cached module spec must not hide removal.
    assert not tool.status(None).installed
    with pytest.raises(StepFailed, match="openmm"):
        plan[-1].call()


def test_pdbfixer_status_reports_an_unloadable_dependency(monkeypatch, tmp_path):
    import amdockvs.integrations.registry as tools

    origin = tmp_path / "__init__.py"
    origin.write_text("")
    monkeypatch.setattr(tools.importlib.util, "find_spec", lambda _name: SimpleNamespace(origin=str(origin)))

    def broken(_module):
        raise ImportError("OpenMM shared library is missing")

    monkeypatch.setattr(tools.importlib, "import_module", broken)
    status = tools.get_tool("pdbfixer").status(None)
    assert not status.installed and "shared library" in status.message


@pytest.mark.parametrize("name", ["pdbfixer", "openmm"])
def test_protein_package_removal_uses_pip_and_preserves_other_packages(monkeypatch, tmp_path, name):
    import sys
    import threading

    import amdockvs.integrations.registry as tools

    package = tmp_path / name
    package.mkdir()
    origin = package / "__init__.py"
    origin.write_text("")
    monkeypatch.setattr(tools.importlib.util, "find_spec", lambda _name: SimpleNamespace(origin=str(origin)))
    commands = []
    # Patching run() reaches the whole process, and this fake returns None: a worker thread
    # another test left behind would get None where it expects a CompletedProcess.
    ours, real_run = threading.current_thread(), tools.subprocess.run

    def record(argv, **kwargs):
        if threading.current_thread() is not ours:
            return real_run(argv, **kwargs)
        commands.append(argv)

    monkeypatch.setattr(tools.subprocess, "run", record)
    assert "Removed" in tools.uninstall_tool(None, name)
    assert commands == [[sys.executable, "-m", "pip", "uninstall", "-y", name]]
    assert origin.exists()  # The registry itself never deletes package directories.
