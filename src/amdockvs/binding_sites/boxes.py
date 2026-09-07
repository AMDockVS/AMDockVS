"""Turning coordinates into a docking box (center + size).

A binding site is a box, and every source of one ends here: a reference ligand's
atoms, a residue selection, or a cavity found by :mod:`amdockvs.binding_sites.cavity`.
Pure arithmetic, no RDKit and no PyMOL, so a headless job and the GUI share it.
"""
from __future__ import annotations


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
    # ponytail: one runnable check for the two box builders.
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
