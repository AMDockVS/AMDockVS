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
    # scikit-learn belongs to this environment, not a tools-home directory.
    assert all(status.location is None and status.size_bytes == 0
               for status in statuses if status.tool_id != "scikit-learn")
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


def test_openmm_and_pdbfixer_install_into_their_own_environment(monkeypatch, tmp_path):
    """Both are heavy (OpenMM pulls CUDA) and optional: a recipe env, never this Python."""
    import sys
    import amdockvs.integrations.registry as tools

    monkeypatch.setenv("AMDOCK_TOOLS_HOME", str(tmp_path))
    tool = tools.get_tool("openmm")
    recipe = tool.recipe(None)
    assert tool.category == "Protein preparation"
    assert Path(recipe.prefix).is_relative_to(tmp_path)
    assert "openmm=8.6.1" in recipe.conda and "pdbfixer" in recipe.conda
    assert not tool.status(None).installed
    assert all(sys.executable not in (step.argv or []) for step in tools.install_steps(None, "openmm"))


def test_openmm_runner_repairs_a_structure_in_the_tool_environment(tmp_path):
    """The runner is the only code that imports OpenMM; here this Python stands in for its env."""
    import sys
    from amdockvs.chemistry.tools.receptors import fix_receptor_pdb_file

    pytest.importorskip("pdbfixer")
    source = tmp_path / "gly.pdb"
    # One glycine missing its O: PDBFixer has to add it back.
    source.write_text(
        "ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  GLY A   1       1.458   0.000   0.000  1.00  0.00           C\n"
        "ATOM      3  C   GLY A   1       2.009   1.420   0.000  1.00  0.00           C\n"
        "END\n"
    )
    output = fix_receptor_pdb_file(source_path=source, output_path=tmp_path / "out" / "fixed.pdb",
                                   command=sys.executable)
    assert any(line[12:16].strip() == "O" for line in output.read_text().splitlines() if line.startswith("ATOM"))
