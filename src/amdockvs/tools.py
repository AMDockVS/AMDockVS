from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolArtifact:
    kind: str
    path: Path | None = None
    media_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    artifact: ToolArtifact | None = None
    payload: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def path(self) -> Path | None:
        return None if self.artifact is None else self.artifact.path


__all__ = ["ToolArtifact", "ToolResult"]
