from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from amdockvs.configuration import app_config


SUPPORTED_MANAGED_TOOLS = {"openbabel", "pkasso"}


@dataclass(frozen=True)
class ProtonationToolStatus:
    name: str
    version: str
    installed: bool
    prefix: Path
    command: Path | None
    message: str


def _data_home() -> Path:
    configured = str(os.environ.get("AMDOCK_TOOLS_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_data = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    root = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return (root / "AMDockVS" / "tools").resolve()


def _tool_config(runtime, name: str):
    config = app_config(runtime).protonation
    if name == "openbabel":
        return config.openbabel
    if name == "pkasso":
        return config.pkasso
    raise ValueError(f"Unsupported managed protonation tool: {name}")


def managed_prefix(runtime, name: str) -> Path:
    tool = _tool_config(runtime, name)
    configured = str(tool.prefix or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (_data_home() / "protonation" / name / str(tool.version)).resolve()


def prefix_python(prefix: Path) -> Path:
    return prefix / ("python.exe" if os.name == "nt" else "bin/python")


def _prefix_command(prefix: Path, name: str) -> Path:
    executable = f"{name}.exe" if os.name == "nt" else name
    return prefix / ("Scripts" if os.name == "nt" else "bin") / executable


def find_environment_manager() -> Path | None:
    candidates: list[Path] = []
    for variable in ("AMDOCK_ENV_MANAGER", "MAMBA_EXE", "CONDA_EXE"):
        configured = str(os.environ.get(variable) or "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
    prefix = Path(sys.prefix).expanduser().resolve()
    if prefix.parent.name == "envs":
        base = prefix.parents[1]
        for relative in ("bin/micromamba", "bin/mamba", "bin/conda", "condabin/conda"):
            candidates.append(base / relative)
    for name in ("micromamba", "mamba", "conda"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def protonation_tool_status(runtime, name: str) -> ProtonationToolStatus:
    normalized = str(name or "").strip().lower()
    tool = _tool_config(runtime, normalized)
    prefix = managed_prefix(runtime, normalized)
    if normalized == "openbabel":
        override = str(os.environ.get("AMDOCK_OPENBABEL") or "").strip()
        command = Path(override).expanduser().resolve() if override else _prefix_command(prefix, "obabel")
        if not command.is_file():
            on_path = shutil.which("obabel")
            command = Path(on_path).resolve() if on_path else command
    else:
        command = prefix_python(prefix)
    installed = command.is_file() and os.access(command, os.X_OK)
    if installed and normalized == "pkasso":
        try:
            check = subprocess.run(
                [str(command), "-c", "import importlib.metadata as m; print(m.version('pkasso'))"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            installed = check.returncode == 0
        except (OSError, subprocess.SubprocessError):
            installed = False
    label = "OpenBabel" if normalized == "openbabel" else "pKasso"
    message = f"{label} {tool.version} is ready." if installed else f"{label} {tool.version} is not installed."
    return ProtonationToolStatus(
        name=normalized,
        version=str(tool.version),
        installed=installed,
        prefix=prefix,
        command=command if installed else None,
        message=message,
    )


def _run_install(command: list[str], *, timeout: int) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Environment installation failed: {detail}")


def install_protonation_tool(runtime, name: str) -> ProtonationToolStatus:
    normalized = str(name or "").strip().lower()
    current = protonation_tool_status(runtime, normalized)
    if current.installed:
        return current
    manager = find_environment_manager()
    if manager is None:
        raise RuntimeError(
            "No micromamba, mamba, or conda executable was found. Install micromamba or set "
            "AMDOCK_ENV_MANAGER to its executable path."
        )
    prefix = managed_prefix(runtime, normalized)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    tool = _tool_config(runtime, normalized)
    base_command = [str(manager), "create", "--yes", "--prefix", str(prefix), "--channel", "conda-forge"]
    if normalized == "openbabel":
        _run_install([*base_command, f"openbabel={tool.version}"], timeout=1800)
    else:
        _run_install([*base_command, "python=3.12", "pip"], timeout=1800)
        python = prefix_python(prefix)
        _run_install(
            [str(python), "-m", "pip", "install", f"pkasso[unipka]=={tool.version}"],
            timeout=3600,
        )
    installed = protonation_tool_status(runtime, normalized)
    if not installed.installed:
        raise RuntimeError(f"{normalized} installation completed but its executable is missing.")
    return installed


__all__ = [
    "ProtonationToolStatus",
    "find_environment_manager",
    "install_protonation_tool",
    "managed_prefix",
    "prefix_python",
    "protonation_tool_status",
]
