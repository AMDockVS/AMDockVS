"""Public runtime facade for optional external tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ms_flow.core.executor.provisioning import run_steps

from amdockvs.integrations.registry import (
    ToolStatus,
    get_tool,
    install_steps,
    tool_statuses,
    uninstall_tool,
)


@dataclass
class ToolAPI:
    runtime: Any

    def status(self, tool_id: str | None = None) -> ToolStatus | tuple[ToolStatus, ...]:
        """Return the current readiness of one managed tool, or every tool."""
        if tool_id is None:
            return tuple(tool_statuses(self.runtime))
        return get_tool(tool_id).status(self.runtime)

    def install(self, tool_id: str) -> ToolStatus:
        """Install a tool if needed and return its verified status."""
        current = self.status(tool_id)
        assert isinstance(current, ToolStatus)
        if not current.installed:
            run_steps(install_steps(self.runtime, tool_id))
        result = self.status(tool_id)
        assert isinstance(result, ToolStatus)
        return result

    def uninstall(self, tool_id: str) -> str:
        """Remove a managed tool from disk."""
        return uninstall_tool(self.runtime, tool_id)

    def repair(self, tool_id: str) -> ToolStatus:
        """Remove and reinstall a tool whose on-disk installation is suspect."""
        self.uninstall(tool_id)
        return self.install(tool_id)


__all__ = ["ToolAPI"]
