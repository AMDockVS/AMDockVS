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

import os
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
                # Written aside and renamed, as ms_adtools' own fetch does: preparations run
                # in parallel over a shared cache, and a reader that opens a half-copied CIF
                # gets a parse error instead of a component.
                part = destination.with_name(f"{destination.name}.{os.getpid()}.part")
                shutil.copy2(source, part)
                os.replace(part, destination)
                seeded.append(code)
    except OSError:
        return []
    return seeded


def nonstandard_resnames(pdb_text: str) -> list[str]:
    """Residue names in a PDB block that ``prepare_receptor`` would have to guess at.

    Everything the CCD path exists for: cofactors, and modified residues the polymer carries.
    Waters and the standard amino acids and nucleotides are ms_adtools' own ``STANDARD`` set.

    A residue of a single atom is left out. What a component contributes is bond orders --
    aromaticity, formal charges, which acid to ionize -- and a lone ion has none of that to
    learn; ms_adtools gives a bare metal charge 0 by design, with or without the CCD. Counting
    them here would make every MG or CA in a structure ask for a component it has no use for,
    and the warning that follows would be noise in front of the cofactor it is there to report.
    """
    from ms_adtools.ccd import STANDARD

    # Per residue instance, not per name: two MG ions in one structure are two monatomic
    # residues, and counting their atoms together would make the pair look like a molecule.
    instances: dict[tuple[str, str], int] = {}
    order: list[str] = []
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if not resname or resname in STANDARD:
            continue
        if resname not in order:
            order.append(resname)
        key = (resname, f"{line[21]}{line[22:27]}")
        instances[key] = instances.get(key, 0) + 1
    biggest: dict[str, int] = {}
    for (resname, _), count in instances.items():
        biggest[resname] = max(biggest.get(resname, 0), count)
    return [name for name in order if biggest.get(name, 0) > 1]


def _links_into_polymer(resname: str) -> bool:
    """Does the CCD call this component a chain-linking residue rather than a free molecule?

    ``_chem_comp.type`` says so: "L-PEPTIDE LINKING", "DNA LINKING" and their kin link, while
    "NON-POLYMER" does not. Unknown counts as linking, so an unresolved name is never accused
    of a fractional charge it may simply not have.
    """
    import gemmi

    from ms_adtools.ccd import component_file

    try:
        path = component_file(resname)
    except Exception:  # noqa: BLE001
        return True
    if path is None:
        return True
    block = gemmi.cif.read(str(path)).sole_block()
    kind = gemmi.cif.as_string(block.find_value("_chem_comp.type") or "").upper()
    return "LINKING" in kind


def fractional_charges(pdbqt_text: str, resnames: list[str], tolerance: float = 0.05):
    """``[(resname, "<chain><resseq>", net)]`` for residues whose net charge is not integral.

    A component typed from the CCD carries whole-number formal charges, so each copy must come
    out on an integer. A fraction means the structure and the component did not line up -- most
    often a heavy atom the structure never modelled, whose charge then has nowhere to go. PLP
    is the case that found this: PDB entries routinely omit its O4A, and the heme-sized error
    that follows (-1.70 where the answer is -2) is invisible in the file itself.

    Reported, not corrected: which atom is missing is the depositor's business, and a receptor
    that is wrong by a third of a charge should say so rather than be quietly patched.
    """
    # Only free components are asked to come out whole. A residue that links into the polymer
    # (a modified amino acid, a modified nucleotide) is drawn by the CCD with the OXT and HXT
    # of its free form, which a chain-internal copy can never have, so its remainder is the
    # linkage and not a defect -- a chain-internal CSO sits at +0.18 and means nothing.
    wanted = {name for name in (str(n).strip().upper() for n in resnames)
              if not _links_into_polymer(name)}
    totals: dict[tuple[str, str], float] = {}
    for line in pdbqt_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in wanted:
            continue
        key = (resname, f"{line[21].strip() or '_'}{line[22:27].strip()}")
        try:
            totals[key] = totals.get(key, 0.0) + float(line[70:76])
        except ValueError:
            continue
    return [(resname, where, net) for (resname, where), net in sorted(totals.items())
            if abs(net - round(net)) > tolerance]


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
    "fractional_charges",
    "nonstandard_resnames",
    "seed_component_cache",
]
