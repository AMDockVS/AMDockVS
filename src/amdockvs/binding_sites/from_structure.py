"""Binding sites derived from a receptor structure at import time.

The components an imported receptor carries — a co-crystal ligand, a coordinated
metal — already name a site. Turning them into `BindingSite` specs is the same
job P2Rank does later, so it lives here and not inside the import pipeline;
`io` calls in, never the other way round.

Pure data in / data out: the caller supplies the parsed components, this returns
the specs the import job persists.
"""
from __future__ import annotations

from typing import Any, Iterable


def binding_site_specs_from_components(
    components: Iterable[dict[str, Any]],
    *,
    box_size: float,
    reference_ligands: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Specs for the kept reference ligands and the coordinated metals.

    `reference_ligands` None means "every ligand component"; a list restricts it to
    the selectors the user kept (artifact copies were dropped upstream).
    """
    size = (float(box_size),) * 3
    reference_set = None if reference_ligands is None else set(reference_ligands)
    specs: list[dict[str, Any]] = []
    for component in components:
        component_class = component["component_class"]
        selector = str(component.get("selector") or "")
        if component_class == "ligand":
            if reference_set is not None and selector not in reference_set:
                continue
            name = f"{component['resname']} site"
        elif component_class == "metal" and component.get("is_coordinated"):
            name = f"{component['resname']} metal site"
        else:
            continue
        specs.append(
            {
                "name": name,
                "source": "ligand" if component_class == "ligand" else "metal",
                "source_ref": component["selector"],
                "center": component["center"],
                "size": size,
                "extra_data": {"component_class": component_class, "selector": component["selector"]},
            }
        )
    return specs


__all__ = ["binding_site_specs_from_components"]
