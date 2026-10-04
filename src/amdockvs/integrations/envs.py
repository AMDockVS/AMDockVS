from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ms_flow.core.executor.provisioning import Step, find_environment_manager
from ms_flow.core.executor.tools import READY, Recipe, ToolSpec, get_install, install_tool, locate, recipe_steps

from amdockvs.core.configuration import app_config
from amdockvs.core.paths import tools_home


SUPPORTED_MANAGED_TOOLS = {"openbabel", "pkasso"}
_TOOL_FILES = Path(__file__).with_name("tools")


@dataclass(frozen=True)
class ProtonationToolStatus:
    name: str
    version: str
    installed: bool
    prefix: Path
    command: Path | None
    message: str


@cache
def tool_spec(name: str) -> ToolSpec:
    return ToolSpec.load(_TOOL_FILES / f"{name}.toml")


def _normalized(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if normalized not in SUPPORTED_MANAGED_TOOLS:
        raise ValueError(f"Unsupported managed protonation tool: {name}")
    return normalized


def protonation_recipe(runtime, name: str, remote_tools_home: str = "") -> Recipe:
    """Recipe for this machine, or for a remote whose tools live under ``remote_tools_home``."""
    normalized = _normalized(name)
    spec = tool_spec(normalized)
    if remote_tools_home:
        return spec.recipe(remote_tools_home)
    # The user-registered locations: a prefix in the config, or an explicit executable.
    configured = str(getattr(app_config(runtime).protonation, normalized).prefix or "").strip()
    override = str(os.environ.get("AMDOCK_OPENBABEL") or "").strip() if normalized == "openbabel" else ""
    return spec.recipe(
        str(tools_home(runtime)),
        prefix=str(Path(configured).expanduser().resolve()) if configured else "",
        extra_paths=[str(Path(override).expanduser().resolve())] if override else [],
        manager=str(find_environment_manager("AMDOCK_ENV_MANAGER") or ""),
    )


def managed_prefix(runtime, name: str) -> Path:
    return Path(protonation_recipe(runtime, name).prefix)


def protonation_tool_status(runtime, name: str) -> ProtonationToolStatus:
    """Runs the tool's check in a subprocess -- call it off the GUI thread."""
    normalized = _normalized(name)
    recipe = protonation_recipe(runtime, normalized)
    state, executable = locate(recipe)
    spec = tool_spec(normalized)
    label, installed = spec.info.get("label", normalized), state == READY
    return ProtonationToolStatus(
        name=normalized,
        version=spec.version,
        installed=installed,
        prefix=Path(recipe.prefix),
        command=Path(executable) if installed else None,
        message=f"{label} {spec.version} is {'ready' if installed else 'not installed'}.",
    )


def protonation_install_steps(runtime, name: str) -> list[Step]:
    return recipe_steps(protonation_recipe(runtime, name))


def install_protonation_tool(runtime, name: str) -> ProtonationToolStatus:
    """Install (or join the install already running) and wait for it. Blocks: not on the GUI thread."""
    install = get_install(install_tool(protonation_recipe(runtime, name)))
    try:
        install.future.result()
    except Exception as exc:
        detail = "\n".join(install.lines[-20:])
        raise RuntimeError(f"Environment installation failed: {exc}\n{detail}") from exc
    return protonation_tool_status(runtime, name)


__all__ = [
    "ProtonationToolStatus",
    "install_protonation_tool",
    "managed_prefix",
    "protonation_install_steps",
    "protonation_recipe",
    "protonation_tool_status",
    "tool_spec",
]
