"""Registry of the external tools AMDock can install on demand.

Docking works without any of them; each one unlocks one optional feature. The
UI half lives in ``amdockvs.ui.settings_tools`` -- this module only knows how to
report status, install and remove.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ms_flow.core.executor.provisioning import Step

from amdockvs.chemistry.protonation_runtime import (
    install_protonation_tool,
    managed_prefix,
    protonation_tool_status,
)
from amdockvs.pockets.p2rank import (
    P2RANK_VERSION,
    ensure_p2rank,
    find_java_command,
    java_major_version,
    p2rank_home,
    p2rank_status,
)


@dataclass(frozen=True)
class ToolStatus:
    tool_id: str
    installed: bool
    message: str
    location: Path | None = None
    size_bytes: int = 0


@dataclass(frozen=True)
class ManagedTool:
    tool_id: str
    label: str
    purpose: str
    footprint: str
    status: Callable[[object], ToolStatus]
    install_steps: Callable[[object], list[Step]]
    location: Callable[[object], Path]


def _directory_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


# --- p2rank ----------------------------------------------------------------

def _p2rank_status(_runtime) -> ToolStatus:
    installation = p2rank_status()
    java = find_java_command()
    ready = installation.installed and int(java_major_version(java) or 0) >= 17
    return ToolStatus(
        tool_id="p2rank",
        installed=ready,
        message=installation.message,
        location=installation.home if installation.installed else None,
        size_bytes=_directory_size(installation.home) if installation.installed else 0,
    )


def _p2rank_steps(_runtime) -> list[Step]:
    steps: list[Step] = []
    if int(java_major_version(find_java_command()) or 0) < 17:
        steps.append(
            Step(
                "install the Java runtime (jdk4py)",
                argv=[sys.executable, "-m", "pip", "install", "jdk4py~=21.0"],
                timeout=900,
            )
        )

    def install() -> str:
        importlib.invalidate_caches()  # the JDK wheel may have landed one step ago
        return ensure_p2rank().message

    steps.append(Step(f"download and extract P2Rank {P2RANK_VERSION}", call=install, timeout=1800))
    return steps


# --- protonation backends --------------------------------------------------

def _protonation_status(name: str) -> Callable[[object], ToolStatus]:
    def status(runtime) -> ToolStatus:
        current = protonation_tool_status(runtime, name)
        return ToolStatus(
            tool_id=name,
            installed=current.installed,
            message=current.message,
            location=current.prefix if current.prefix.is_dir() else None,
            size_bytes=_directory_size(current.prefix),
        )

    return status


def _protonation_steps(name: str) -> Callable[[object], list[Step]]:
    def steps(runtime) -> list[Step]:
        return [
            Step(
                f"create the {name} environment",
                call=lambda: install_protonation_tool(runtime, name).message,
                timeout=3600,
            )
        ]

    return steps


MANAGED_TOOLS: tuple[ManagedTool, ...] = (
    ManagedTool(
        tool_id="p2rank",
        label="P2Rank",
        purpose="Binding-site prediction from a receptor structure.",
        footprint="~400 MB (P2Rank 293 MB + a trimmed Java runtime 101 MB)",
        status=_p2rank_status,
        install_steps=_p2rank_steps,
        location=lambda _runtime: p2rank_home(),
    ),
    ManagedTool(
        tool_id="openbabel",
        label="OpenBabel",
        purpose="pH-aware ligand protonation through the OpenBabel CLI.",
        footprint="~200 MB (isolated conda environment)",
        status=_protonation_status("openbabel"),
        install_steps=_protonation_steps("openbabel"),
        location=lambda runtime: managed_prefix(runtime, "openbabel"),
    ),
    ManagedTool(
        tool_id="pkasso",
        label="pKasso",
        purpose="ML pKa prediction for ligand protonation (MolGpKa / Uni-pKa).",
        footprint="~2 GB (isolated conda environment with PyTorch)",
        status=_protonation_status("pkasso"),
        install_steps=_protonation_steps("pkasso"),
        location=lambda runtime: managed_prefix(runtime, "pkasso"),
    ),
)


def get_tool(tool_id: str) -> ManagedTool:
    normalized = str(tool_id or "").strip().lower()
    for tool in MANAGED_TOOLS:
        if tool.tool_id == normalized:
            return tool
    raise KeyError(f"Unknown managed tool: {tool_id}")


def tool_statuses(runtime) -> list[ToolStatus]:
    """Status of every managed tool. Touches the filesystem -- call it off the GUI thread."""
    return [tool.status(runtime) for tool in MANAGED_TOOLS]


def install_steps(runtime, tool_id: str) -> list[Step]:
    return get_tool(tool_id).install_steps(runtime)


def uninstall_tool(runtime, tool_id: str) -> str:
    """Remove a tool's directory. The shared Java wheel is left alone (pip owns it)."""
    tool = get_tool(tool_id)
    location = tool.location(runtime)
    if not location.is_dir():
        return f"{tool.label} is not installed."
    shutil.rmtree(location)
    return f"Removed {tool.label} from {location}."


__all__ = [
    "MANAGED_TOOLS",
    "ManagedTool",
    "ToolStatus",
    "get_tool",
    "install_steps",
    "tool_statuses",
    "uninstall_tool",
]
