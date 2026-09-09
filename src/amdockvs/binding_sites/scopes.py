"""Serializable query intent for binding-site collections."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BindingSiteScope:
    molecule_id: int | None = None
    source: str | None = None
    run_id: str | None = None
    limit: int | None = None


__all__ = ["BindingSiteScope"]
