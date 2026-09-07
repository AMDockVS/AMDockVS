"""Reads of `BindingSite` rows that other features need.

Docking asks "which box does this receptor dock into?" on every run and every
readiness check; that single FK hop lives here so `binding_sites` stays the only
package that knows the table.
"""
from __future__ import annotations

from typing import Any

from amdockvs.models import BindingSite


def active_site(session, receptor) -> BindingSite | None:
    """The receptor's active site. A single FK hop: there is no index to match against."""
    site_id = int(getattr(receptor, "active_binding_site_id", 0) or 0)
    return None if site_id <= 0 else session.get(BindingSite, site_id)


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


__all__ = ["active_site", "site_payload"]
