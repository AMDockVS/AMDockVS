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

def centroid(coords: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    n = len(coords)
    if n == 0:
        raise ValueError("no coordinates")
    sx = sum(c[0] for c in coords)
    sy = sum(c[1] for c in coords)
    sz = sum(c[2] for c in coords)
    return (sx / n, sy / n, sz / n)


def radius_of_gyration(coords: list[tuple[float, float, float]]) -> float:
    """Unweighted (geometric) radius of gyration: RMS distance of atoms to the centroid."""
    n = len(coords)
    if n == 0:
        raise ValueError("no coordinates")
    cx, cy, cz = centroid(coords)
    sq = sum((c[0] - cx) ** 2 + (c[1] - cy) ** 2 + (c[2] - cz) ** 2 for c in coords)
    return (sq / n) ** 0.5


def box_edge_from_rg(
    rg: float,
    *,
    scale: float = 1.5,
    padding: float = 4.0,
    minimum: float = 12.0,
    maximum: float = 30.0,
) -> float:
    """Cubic box edge (Å) for a ligand of gyration radius ``rg``.

    edge = 2*(scale*rg + padding), clamped. The ligand spans roughly a few*rg; the
    factor leaves room for translational/rotational sampling.
    ponytail: scale/padding are a heuristic — expose them as knobs and calibrate
    against redocking RMSD if the default box turns out too tight/loose.
    """
    edge = 2.0 * (scale * float(rg) + padding)
    return max(minimum, min(maximum, edge))


def box_from_coords(
    coords: list[tuple[float, float, float]],
    *,
    scale: float = 1.5,
    padding: float = 4.0,
) -> dict:
    """{center, size, rg}: center = centroid, size = cubic box from radius of gyration."""
    rg = radius_of_gyration(coords)
    edge = box_edge_from_rg(rg, scale=scale, padding=padding)
    return {"center": centroid(coords), "size": (edge, edge, edge), "rg": rg}


def _normalize3(v: tuple[float, float, float]) -> tuple[float, float, float]:
    mag = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    if mag < 1e-9:
        return (0.0, 0.0, 1.0)
    return (v[0] / mag, v[1] / mag, v[2] / mag)


def _sphere_points(
    center: tuple[float, float, float],
    *,
    n_atoms: int,
    radius: float,
) -> list[tuple[float, float, float]]:
    """A compact 3D marker cloud: ``center`` plus points spread evenly over a sphere
    (Fibonacci lattice). Orientation-independent — reads as a small blob at the box anchor,
    not a flat disc. Purely a visualization object."""
    import math

    n_atoms = max(1, int(n_atoms))
    if n_atoms == 1:
        return [center]
    shell = n_atoms - 1
    golden = math.pi * (3.0 - math.sqrt(5.0))
    points = [center]
    for i in range(shell):
        y = 1.0 - (i / max(1, shell - 1)) * 2.0 if shell > 1 else 0.0
        r = math.sqrt(max(0.0, 1.0 - y * y))
        theta = golden * i
        points.append((
            center[0] + math.cos(theta) * r * radius,
            center[1] + y * radius,
            center[2] + math.sin(theta) * r * radius,
        ))
    return points


def pseudo_ligand_box(
    receptor_coords: list[tuple[float, float, float]],
    selection_coords: list[tuple[float, float, float]],
    *,
    n_atoms: int = 8,
    spread: float = 3.0,
    padding: float = 5.0,
    surface_push: float = 3.0,
    default_edge: float = 22.5,
    max_edge: float = 30.0,
) -> dict:
    """{center, size, points}: a docking box over the selected residues.

    Center = selection centroid nudged a *bounded* few Å toward the protein surface (along the
    protein-COM→selection direction) so the box sits at the pocket mouth instead of buried —
    the old AutoLigand goal, but a bounded push, not a global convex-hull projection (which
    overshot deep pockets to the outer shell). Size only ever GROWS to enclose a wide
    selection; a single/buried residue keeps the sensible ``default_edge`` box instead of
    collapsing to something unusably small. ``points`` is a 3D marker cloud for visualization
    only — it does not drive the size.
    """
    if not selection_coords:
        raise ValueError("pseudo_ligand_box needs at least one selection atom.")
    sel_c = centroid(selection_coords)
    prot_c = centroid(receptor_coords) if receptor_coords else sel_c
    outward = _normalize3((sel_c[0] - prot_c[0], sel_c[1] - prot_c[1], sel_c[2] - prot_c[2]))
    center = (
        sel_c[0] + outward[0] * float(surface_push),
        sel_c[1] + outward[1] * float(surface_push),
        sel_c[2] + outward[2] * float(surface_push),
    )
    xs = [c[0] for c in selection_coords]
    ys = [c[1] for c in selection_coords]
    zs = [c[2] for c in selection_coords]
    extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    edge = max(float(default_edge), min(float(max_edge), extent + 2.0 * float(padding)))
    points = _sphere_points(center, n_atoms=n_atoms, radius=spread)
    return {"center": center, "size": (edge, edge, edge), "points": points}


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

    # box geometry: a cube of atoms centered at origin -> centroid ~0, edge clamped >= minimum.
    cube = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]
    box = box_from_coords(cube)
    assert all(abs(v) < 1e-9 for v in box["center"]), box["center"]
    assert abs(box["rg"] - 3.0 ** 0.5) < 1e-9, box["rg"]
    assert abs(box["size"][0] - 2.0 * (1.5 * 3.0 ** 0.5 + 4.0)) < 1e-9, box["size"]
    assert box_edge_from_rg(0.1) == 12.0, box_edge_from_rg(0.1)  # tiny -> clamped to minimum
    assert box_edge_from_rg(5.0) == 23.0, box_edge_from_rg(5.0)  # 2*(1.5*5+4)
    assert box_edge_from_rg(20.0) == 30.0, box_edge_from_rg(20.0)  # huge -> clamped to maximum

    # pseudo-ligand box: center is the selection centroid pushed a bounded few A outward
    # (toward the surface), never overshooting to the outer shell; size encloses the selection.
    from math import cos as _cos, sin as _sin

    shell = [
        (10.0 * _sin(t) * _cos(p), 10.0 * _sin(t) * _sin(p), 10.0 * _cos(t))
        for t in [i * 3.14159 / 6 for i in range(7)]
        for p in [j * 3.14159 / 6 for j in range(12)]
    ]
    buried = [(2.0, 0.0, 0.0), (2.5, 0.5, 0.0)]  # off-center residue, deep inside
    pbox = pseudo_ligand_box(shell, buried, n_atoms=8, spread=2.0, surface_push=3.0)
    sel_r = sum(((buried[0][k] + buried[1][k]) / 2.0) ** 2 for k in range(3)) ** 0.5
    center_r = sum(c ** 2 for c in pbox["center"]) ** 0.5
    assert center_r > sel_r, (center_r, sel_r)  # pushed toward the surface, bounded
    assert center_r < sel_r + 3.5, (center_r, sel_r)  # but NOT overshooting to the outer shell
    assert len(pbox["points"]) == 8, len(pbox["points"])
    assert pbox["size"][0] == 22.5, pbox["size"]  # tiny selection -> keeps default edge, no shrink
    # a wide multi-residue selection GROWS the box past the default.
    wide = [(0.0, 0.0, 0.0), (24.0, 0.0, 0.0)]
    assert pseudo_ligand_box(shell, wide)["size"][0] > 22.5, "wide selection should enlarge box"
    # points form a 3D blob, not a flat disc: they span all three axes.
    pts = pbox["points"]
    for axis in range(3):
        span = max(p[axis] for p in pts) - min(p[axis] for p in pts)
        assert span > 0.5, (axis, span)
    print("pseudo_ligand_box OK", tuple(round(v, 2) for v in pbox["center"]))
    print("box_from_coords OK", box)
