"""The molecule scope: a query intent (filters + set + order + limit), not a result.

Every feature that operates on "some molecules" takes one of these and turns it
into a QuerySpec at the last moment, so nothing materialises ids in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ms_flow.selection import Selection


@dataclass(frozen=True)
class MoleculeScope:
    filters: dict[str, Any] = field(default_factory=dict)
    source_set_id: int | None = None
    order: tuple[str, ...] = ("id",)
    limit: int | None = None


def as_molecule_scope(value: MoleculeScope | Selection[Any] | None) -> MoleculeScope | None:
    """Extract the serializable molecule query from a fluent selection."""
    if isinstance(value, Selection):
        value = value.scope
    if value is not None and not isinstance(value, MoleculeScope):
        raise TypeError(f"Expected MoleculeScope or Selection, got {type(value).__name__}.")
    return value


def is_molecule_scope(value: object) -> bool:
    return isinstance(value, MoleculeScope) or (
        isinstance(value, Selection) and isinstance(value.scope, MoleculeScope)
    )


def scope_payload(scope: MoleculeScope | Selection[Any] | None) -> dict[str, Any]:
    scope = as_molecule_scope(scope)
    if scope is None:
        return {}
    return {
        "filters": dict(scope.filters or {}),
        "source_set_id": scope.source_set_id,
        "order": list(scope.order or ("id",)),
        "limit": scope.limit,
    }


__all__ = ["MoleculeScope", "as_molecule_scope", "is_molecule_scope", "scope_payload"]
