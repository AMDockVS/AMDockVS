"""CCD components for the non-standard residues a receptor keeps.

``ms_adtools.prepare_receptor`` guesses a bare cofactor's chemistry from geometry alone: no bond
orders, so no aromaticity, no formal charges and no ionized acids. On FMN that came out as 31
plain-``C``/``N`` atoms with a net charge of +0.001, where the isoalloxazine ring is aromatic and
the 5'-phosphate is a dianion — the flavin's own donor/acceptor pattern was inverted at N1 and
N10. ``ccd=True`` fixes all of it from the component's bond orders.

What that costs is a dependency on the RCSB at preparation time: ``component_file`` fetches a
component it has not cached, and **returns None offline, silently**, leaving the residue with the
guessed values. A docking run on a mis-charged cofactor looks exactly like a good one, so:

* the components of the cofactors we expect are shipped in ``data/ccd`` and seeded into the cache,
  which makes the common path offline and pins it for a reproducible run;
* anything else still fetches once, as upstream does;
* and :func:`components_without_templates` reports what neither route resolved, so the caller can
  warn instead of quietly docking against a guess.

Shipping the component rather than a hand-written charge table is deliberate: the CCD is the
authority on bond orders, so adding a cofactor is dropping its ``<RESNAME>.cif`` in ``data/ccd``,
never deriving charges by hand.
"""
from __future__ import annotations

import shutil
from pathlib import Path

#: Shipped CCD components, seeded into the ms_adtools cache. Add a cofactor by adding its CIF
#: (https://files.rcsb.org/ligands/download/<RESNAME>.cif), not by editing code.
BUNDLED_COMPONENT_DIR = Path(__file__).resolve().parents[1].parent / "data" / "ccd"


def bundled_components() -> dict[str, Path]:
    """``{RESNAME: cif}`` of the components shipped with AMDockVS."""
    if not BUNDLED_COMPONENT_DIR.is_dir():
        return {}
    return {path.stem.upper(): path for path in sorted(BUNDLED_COMPONENT_DIR.glob("*.cif"))}


def seed_component_cache() -> list[str]:
    """Copy the shipped components into the ms_adtools CCD cache, and report what was added.

    ``ccd=True`` reads that cache before it reaches the network, so a shipped component is the
    one that gets used: offline, and the same bytes on every machine. Only missing names are
    written — a component already cached (fetched by any molsuite tool) is left alone, and a
    cache that cannot be written to is not fatal, it only means the fetch path is used.
    """
    bundled = bundled_components()
    if not bundled:
        return []
    from ms_adtools.ccd import cache_dir

    seeded: list[str] = []
    try:
        target = cache_dir()
        target.mkdir(parents=True, exist_ok=True)
        for code, source in bundled.items():
            destination = target / f"{code}.cif"
            if not destination.exists():
                shutil.copy2(source, destination)
                seeded.append(code)
    except OSError:
        return []
    return seeded


def nonstandard_resnames(pdb_text: str) -> list[str]:
    """Residue names in a PDB block that ``prepare_receptor`` would have to guess at.

    Everything the CCD path exists for: cofactors, and modified residues the polymer carries.
    Waters and the standard amino acids and nucleotides are ms_adtools' own ``STANDARD`` set.
    """
    from ms_adtools.ccd import STANDARD

    found: list[str] = []
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname and resname not in STANDARD and resname not in found:
            found.append(resname)
    return found


def components_without_templates(resnames: list[str]) -> list[str]:
    """Of ``resnames``, those with no CCD component from the cache, the bundle or the RCSB.

    Each of these keeps the geometry-guessed types and charges, which for anything with an
    ionizable group or an aromatic ring is wrong in a way no later step reports. Resolving a
    name here also caches it, so the preparation that follows does not fetch it again.
    """
    from ms_adtools.ccd import component_file

    missing: list[str] = []
    for resname in resnames:
        try:
            resolved = component_file(resname)
        except Exception:  # noqa: BLE001 - an unreachable RCSB must not sink the preparation
            resolved = None
        if resolved is None:
            missing.append(resname)
    return missing


__all__ = [
    "BUNDLED_COMPONENT_DIR",
    "bundled_components",
    "components_without_templates",
    "nonstandard_resnames",
    "seed_component_cache",
]
