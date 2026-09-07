"""The molecule scope: a query intent (filters + set + order + limit), not a result.

Every feature that operates on "some molecules" takes one of these and turns it
into a QuerySpec at the last moment, so nothing materialises ids in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MoleculeScope:
    filters: dict[str, Any] = field(default_factory=dict)
    source_set_id: int | None = None
    order: tuple[str, ...] = ("id",)
    limit: int | None = None


def scope_payload(scope: MoleculeScope | None) -> dict[str, Any]:
    if scope is None:
        return {}
    return {
        "filters": dict(scope.filters or {}),
        "source_set_id": scope.source_set_id,
        "order": list(scope.order or ("id",)),
        "limit": scope.limit,
    }


__all__ = ["MoleculeScope", "scope_payload"]
