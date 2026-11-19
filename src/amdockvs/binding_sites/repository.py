"""Reads of `BindingSite` rows that other features need.

Docking asks "which box does this receptor dock into?" on every run and every
readiness check; that lookup lives here so `binding_sites` stays the only
package that knows the table.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, func, literal, update
from sqlalchemy.orm import aliased
from sqlmodel import select

from amdockvs.models import BindingSite


def active_sites(session, molecule_id: int) -> list[BindingSite]:
    """Every site of the molecule flagged `is_active`, oldest first: the sites it docks into.

    A receptor with three of them is docked three times, once per site."""
    return list(session.exec(
        select(BindingSite)
        .where(BindingSite.molecule_id == int(molecule_id))
        .where(BindingSite.is_active == True)  # noqa: E712 - SQL expression
        .order_by(BindingSite.id)
    ).all())


def active_site(session, receptor) -> BindingSite | None:
    """The receptor's first active site: what a single-box view (the 3D box, a readiness check)
    shows. Docking reads `active_sites` instead — there may be several."""
    return session.exec(
        select(BindingSite)
        .where(BindingSite.molecule_id == int(receptor.id or 0))
        .where(BindingSite.is_active == True)  # noqa: E712 - SQL expression
        .order_by(BindingSite.id)
    ).first()


def set_active(session, molecule_id: int, site_id: int | None) -> None:
    """Make `site_id` the molecule's only active site (None: none active). The caller commits."""
    session.exec(update(BindingSite).where(BindingSite.molecule_id == int(molecule_id))
                 .where(BindingSite.is_active == True).values(is_active=False))  # noqa: E712
    if site_id is not None:
        session.exec(update(BindingSite).where(BindingSite.id == int(site_id)).values(is_active=True))


def set_site_active(session, site_id: int, active: bool) -> None:
    """Turn one site on or off, leaving the molecule's other sites alone: a checkbox, not a
    radio button. The caller commits."""
    session.exec(update(BindingSite).where(BindingSite.id == int(site_id)).values(is_active=bool(active)))


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


def site_name_expr(model):
    """The name of the site a row points at, as a correlated subquery: for a table that shows
    `binding_site_id` as a name. NULL where the row matches no site, which is the plain rigid
    receptor; the caller decides what to print there."""
    site = aliased(BindingSite)
    return (
        select(func.coalesce(func.nullif(site.name, ""), literal("BS ") + cast(site.id, String)))
        .where(site.id == model.binding_site_id)
        .correlate(model)
        .scalar_subquery()
    )


__all__ = ["active_site", "active_sites", "set_active", "set_site_active", "site_name_expr",
           "site_payload"]
