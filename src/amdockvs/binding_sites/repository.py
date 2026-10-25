"""Reads of `BindingSite` rows that other features need.

Docking asks "which box does this receptor dock into?" on every run and every
readiness check; that lookup lives here so `binding_sites` stays the only
package that knows the table.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlmodel import select

from amdockvs.models import BindingSite


def active_site(session, receptor) -> BindingSite | None:
    """The receptor's active site: the one flagged `is_active` (at most one, a partial UNIQUE)."""
    return session.exec(
        select(BindingSite)
        .where(BindingSite.molecule_id == int(receptor.id or 0))
        .where(BindingSite.is_active == True)  # noqa: E712 - SQL expression
    ).first()


def set_active(session, molecule_id: int, site_id: int | None) -> None:
    """Make `site_id` the molecule's active site (None: no active site). The caller commits.

    Two statements: SQLite checks the partial UNIQUE row by row, so flipping both in one UPDATE
    could meet two active rows half-way."""
    session.exec(update(BindingSite).where(BindingSite.molecule_id == int(molecule_id))
                 .where(BindingSite.is_active == True).values(is_active=False))  # noqa: E712
    if site_id is not None:
        session.exec(update(BindingSite).where(BindingSite.id == int(site_id)).values(is_active=True))


def site_payload(site: BindingSite | None) -> dict[str, Any] | None:
    """The box as a plain mapping for job params and table rows."""
    if site is None or not bool(site.is_defined):
        return None
    return {
        "binding_site_id": int(site.id or 0) or None,
        "center": [float(site.center_x or 0.0), float(site.center_y or 0.0), float(site.center_z or 0.0)],
        "size": [float(site.size_x or 0.0), float(site.size_y or 0.0), float(site.size_z or 0.0)],
        "spacing": float(dict(site.extra_data or {}).get("spacing") or 0.375),
    }


__all__ = ["active_site", "set_active", "site_payload"]
