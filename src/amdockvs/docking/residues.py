"""Enumerate receptor residues that fall inside a docking box.

Flexible-residue selection draws its candidates from the residues that have at
least one atom inside the active binding-site box, so the user picks from a
handful instead of the receptor's hundreds. Pure PDB/PDBQT column parsing — no
Meeko/prody dependency — so it stays cheap and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoxResidue:
    chain: str
    resname: str
    resnum: int

    @property
    def key(self) -> str:
        # chain:resname:resnum — the stable id we persist and hand to the prep step.
        return f"{self.chain or '_'}:{self.resname}:{self.resnum}"

    @property
    def label(self) -> str:
        return f"{self.chain or '-'} · {self.resname} {self.resnum}"


# Residues with no rotatable side chain — flexibilizing them is pointless: GLY has none,
# ALA's only side-chain atom (CB) has no rotatable bond, PRO's ring is locked.
NON_ROTATABLE_RESNAMES = frozenset({"GLY", "PRO", "ALA"})


def residues_in_box(
    structure_text: str,
    center: tuple[float, float, float],
    size: tuple[float, float, float],
) -> list[BoxResidue]:
    """Residues fully inside the axis-aligned box (center ± size/2), excluding
    non-rotatable residues (GLY/PRO/ALA).

    "Fully inside" = every atom of the residue is within the box, so a flexible
    side chain can't swing outside the searched volume. Reads ATOM/HETATM in
    PDB/PDBQT fixed-column format; malformed lines are skipped, not fatal.
    """
    half = (abs(size[0]) / 2.0, abs(size[1]) / 2.0, abs(size[2]) / 2.0)
    lo = (center[0] - half[0], center[1] - half[1], center[2] - half[2])
    hi = (center[0] + half[0], center[1] + half[1], center[2] + half[2])

    seen: dict[tuple[str, int, str], BoxResidue] = {}
    outside: set[tuple[str, int, str]] = set()
    for line in structure_text.splitlines():
        if not (line.startswith("ATOM") or line.startswith("HETATM")):
            continue
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except (ValueError, IndexError):
            continue
        resname = line[17:20].strip()
        if resname in NON_ROTATABLE_RESNAMES:
            continue
        chain = line[21:22].strip()
        try:
            resnum = int(line[22:26])
        except (ValueError, IndexError):
            continue
        key = (chain, resnum, resname)
        if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1] and lo[2] <= z <= hi[2]):
            outside.add(key)  # one atom out → whole residue is not fully inside
            continue
        if key not in seen:
            seen[key] = BoxResidue(chain=chain, resname=resname, resnum=resnum)
    return sorted(
        (r for key, r in seen.items() if key not in outside),
        key=lambda r: (r.chain, r.resnum, r.resname),
    )


# ---------------------------------------------------------------------------
# Auto box geometry — derive a docking box from a reference ligand's extent.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # ponytail: one runnable check — only residues with ALL atoms inside survive, GLY/PRO/ALA
    # are dropped, and a residue with one atom poking out is rejected.
    def atom(serial, name, resname, chain, resnum, x, y, z):
        return (
            f"ATOM  {serial:>5} {name:<4} {resname:<3} {chain}{resnum:>4}    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00"
        )

    sample = "\n".join([
        atom(1, "N", "TYR", "A", 1, 10.0, 10.0, 10.0),   # TYR fully inside -> kept
        atom(2, "CA", "TYR", "A", 1, 10.5, 10.5, 10.5),
        atom(3, "N", "TRP", "B", 7, 11.0, 11.0, 11.0),   # TRP one atom out -> rejected
        atom(4, "CZ", "TRP", "B", 7, 99.0, 99.0, 99.0),
        atom(5, "CB", "ALA", "A", 2, 10.0, 10.0, 10.0),  # ALA non-rotatable -> dropped
        atom(6, "CA", "GLY", "A", 3, 10.0, 10.0, 10.0),  # GLY non-rotatable -> dropped
        "GARBAGE LINE THAT SHOULD BE IGNORED",
    ])
    res = residues_in_box(sample, center=(10.0, 10.0, 10.0), size=(4.0, 4.0, 4.0))
    keys = [r.key for r in res]
    assert keys == ["A:TYR:1"], keys
    print("residues_in_box OK", keys)
