"""Formal charges for the bare metal ions of a receptor, and what that does and does not buy.

Gasteiger has no parameters for a metal, so RDKit returns NaN and ms_adtools writes 0, as
AutoDockTools does. A zero-charge ion is invisible to the electrostatic term: a carboxylate
feels nothing at all toward a catalytic Mg(2+). Giving the ion its oxidation state is the
cheaper of two wrongs -- the attraction exists, even if its shape does not.

What it does not buy is coordination. Metal binding is directional: a tetrahedral zinc wants
its ligand on an axis, not merely nearby, and a point charge cannot say so. AutoDock4Zn exists
because the point charge alone was not enough; it places TZ pseudo-atoms that carry the
geometry, and AMDockVS has that path (``docking.methods`` ZINC, ``engines.autogrid`` zinc=True)
-- for zinc, and for nothing else. So a receptor whose site holds any other metal is reported:
the number is there, the geometry is not, and that is worth knowing before reading a score.

Zinc itself is left at 0. AutoDock4Zn was parameterized and validated on receptors prepared the
ADT way, zinc uncharged, with the TZ pseudo-atoms carrying the interaction; adding a +2 on top
would change a calibrated protocol rather than fill a gap, so zinc does not take part here.

Two further caveats belong with the number. The protein's own Gasteiger charges were computed
without the metal present, so the polarisation a real ion induces on its coordinating residues
is missing and a full formal charge double-counts what those residues should have carried. And
AD4's electrostatic and desolvation terms were calibrated against receptors prepared the other
way, at 0. This is a deliberate trade, not a correction -- validate it on a known complex
before trusting a ranking that depends on it.

Charges come from the Chemical Component Dictionary, so an entry's own code decides its
oxidation state: FE is Fe(III) and FE2 is Fe(II), CU is Cu(II) and CU1 is Cu(I).
"""
from __future__ import annotations

#: Residue names this applies to: the coordination metals AMDockVS keeps in a PDBQT, with the
#: oxidation-state variants of the same elements. Identified by residue name and never by the
#: last column of a PDBQT, which holds the AutoDock *type*, not the element -- "NA" there means
#: an acceptor nitrogen, not sodium, so reading it as an element asks the RCSB about every ALA,
#: ARG, HIS and LYS in the receptor.
#:
#: The alkali and halide ions are deliberately absent. AD4 has no type for them, their
#: components are not shipped, and charging them would land at +1 online and 0 offline.
#: ZN is absent too: AutoDock4Zn models it, at 0 (see the module note).
BARE_METAL_RESNAMES = frozenset({"FE", "FE2", "MN", "MG", "CA", "CU", "CU1"})


def _monatomic_metal_rows(pdbqt_text: str) -> dict[tuple[str, str], list[int]]:
    """``{(resname, "<chain><resseq>"): [line index]}`` for residues that are one metal atom.

    Every atom of a residue is counted, not only the metal-looking ones: a residue is a bare
    ion when it holds exactly one atom in total. Counting matches alone would call the lone
    iron of a heme a bare ion and charge it twice.
    """
    by_residue: dict[tuple[str, str], list[int]] = {}
    for index, line in enumerate(pdbqt_text.splitlines()):
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
            continue
        key = (line[17:20].strip().upper(), f"{line[21].strip() or '_'}{line[22:27].strip()}")
        by_residue.setdefault(key, []).append(index)
    return {key: rows for key, rows in by_residue.items()
            if len(rows) == 1 and key[0] in BARE_METAL_RESNAMES}


def formal_charge(resname: str) -> float | None:
    """The CCD's own charge for a one-atom component, or None if it does not resolve."""
    import gemmi

    from ms_adtools.ccd import component_file

    try:
        path = component_file(resname)
    except Exception:  # noqa: BLE001 - an unreachable RCSB must not sink the preparation
        return None
    if path is None:
        return None
    block = gemmi.cif.read(str(path)).sole_block()
    rows = list(block.find("_chem_comp_atom.", ["atom_id", "charge"]))
    if len(rows) != 1:  # not a bare ion after all; leave it to the component path
        return None
    value = gemmi.cif.as_string(rows[0][1])
    try:
        return float(value)
    except ValueError:
        return None


def apply_formal_charges(pdbqt_text: str):
    """Give every bare metal ion its CCD charge. Returns ``(text, applied, warnings)``.

    ``applied`` is ``{resname: charge}`` for what changed. ``warnings`` names the metals that
    got a charge but have no coordination model, which is every one of them except zinc, and
    only when the receptor actually holds one.
    """
    found = _monatomic_metal_rows(pdbqt_text)
    if not found:
        return pdbqt_text, {}, []

    charges: dict[str, float | None] = {}
    for resname, _ in found:
        if resname not in charges:
            charges[resname] = formal_charge(resname)

    lines = pdbqt_text.splitlines(True)
    applied: dict[str, float] = {}
    for (resname, _), rows in found.items():
        charge = charges.get(resname)
        if charge is None or charge == 0.0:
            continue
        for index in rows:
            line = lines[index].rstrip("\n")
            lines[index] = f"{line[:70]}{charge:>6.3f}{line[76:]}\n"
        applied[resname] = charge

    warnings: list[str] = []
    uncovered = sorted(applied)  # every charged metal: zinc, the one with a model, is never charged
    if uncovered:
        warnings.append(
            "Only zinc has a coordination model (AutoDock4Zn). "
            + ", ".join(f"{name} ({applied[name]:+.0f})" for name in uncovered)
            + " now carry their formal charge, so a ligand is drawn to them by electrostatics "
            "alone, with nothing to model the direction a coordination bond would take. Treat "
            "poses and scores at those sites as approximate."
        )
    return "".join(lines), applied, warnings


__all__ = [
    "BARE_METAL_RESNAMES",
    "apply_formal_charges",
    "formal_charge",
]
