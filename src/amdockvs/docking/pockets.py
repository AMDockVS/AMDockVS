"""LIGSITE-style pocket detection for docking-box placement.

Given the receptor atoms (coords + vdW radii) and a target selection, snap to the nearest
solvent-accessible cavity and return a pocket-fitted box (center + size) plus a "pseudo-ligand"
point cloud that fills the cavity for visualization.

Pure numpy/scipy — NO PyMOL — so it is unit-testable and reusable from a headless backend job
(a job supplies coords from the receptor file + vdW by element; the GUI supplies them from
PyMOL). This is the fast voxel-grid version of the old AutoLigand idea.

Roles of the knobs (from the original pseudopocket script):
  spacing        voxel resolution in A (precision/speed only)
  vdw_scale      vdW radius scale in the occupancy grid
  probe          solvent probe radius in A (1.4 = water); higher probe/vdw_scale => more
                 enclosed pocket, center pulled toward the mouth
  max_burial     reach (A) of the LIGSITE burial scan
  psp_min        directions (0-7) enclosed to count as a pocket
  search_radius  radius (A) to look for the snap around the target
  center_radius  radius (A) of the local cavity used for the box CENTER
  blob_radius    physical extent (A) of the visualization blob
  n_points       number of blob spheres (density, spread by farthest-point sampling)
"""
from __future__ import annotations

from typing import Sequence


class PocketError(RuntimeError):
    """Raised when no accessible pocket can be found near the target."""


def _ball(radius_vox, np):
    r = int(np.ceil(radius_vox))
    z, y, x = np.ogrid[-r:r + 1, -r:r + 1, -r:r + 1]
    return (x * x + y * y + z * z) <= radius_vox * radius_vox


def _shift(a, vec, np):
    res = np.zeros_like(a)
    src = [slice(None)] * 3
    dst = [slice(None)] * 3
    for ax, v in enumerate(vec):
        if v > 0:
            dst[ax] = slice(v, None); src[ax] = slice(None, -v)
        elif v < 0:
            dst[ax] = slice(None, v); src[ax] = slice(-v, None)
    res[tuple(dst)] = a[tuple(src)]
    return res


def _prot_within(occ, d, maxsteps, np):
    res = np.zeros_like(occ)
    cur = occ
    nd = [-x for x in d]
    for _ in range(maxsteps):
        cur = _shift(cur, nd, np)
        res |= cur
    return res


def _fps(points, start, k, np):
    """Farthest-point sampling: k well-spread points, seeded at ``start``."""
    n = len(points)
    if n <= k:
        return np.arange(n)
    chosen = [start]
    d = np.linalg.norm(points - points[start], axis=1)
    for _ in range(k - 1):
        i = int(d.argmax())
        chosen.append(i)
        d = np.minimum(d, np.linalg.norm(points - points[i], axis=1))
    return np.asarray(chosen)


def pseudo_pocket_box(
    receptor_xyz: Sequence[Sequence[float]],
    receptor_vdw: Sequence[float],
    target_xyz: Sequence[Sequence[float]],
    *,
    spacing: float = 1.0,
    vdw_scale: float = 1.0,
    probe: float = 1.4,
    max_burial: float = 10.0,
    psp_min: int = 4,
    search_radius: float = 14.0,
    center_radius: float = 8.0,
    blob_radius: float = 12.0,
    n_points: int = 120,
    padding: float = 4.0,
    min_edge: float = 18.0,
    max_edge: float = 30.0,
) -> dict:
    """{center, size, points, moved, fallback}: pocket-fitted box over the target selection.

    Raises PocketError if scipy is missing or no accessible pocket is found near the target —
    callers should fall back to a simpler heuristic.
    """
    import numpy as np

    try:
        from scipy import ndimage
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise PocketError("scipy is required for pocket detection (conda install scipy).") from exc

    coords = np.asarray(receptor_xyz, dtype=float)
    radii = np.asarray(receptor_vdw, dtype=float)
    tcoords = np.asarray(target_xyz, dtype=float)
    if coords.ndim != 2 or coords.shape[0] == 0:
        raise PocketError("empty receptor selection.")
    if tcoords.ndim != 2 or tcoords.shape[0] == 0:
        raise PocketError("empty target selection.")
    if radii.shape[0] != coords.shape[0]:
        radii = np.full(coords.shape[0], 1.7)  # carbon-ish default if vdW missing

    spacing = float(spacing)
    target_center = tcoords.mean(axis=0)

    half = search_radius + blob_radius + probe + 3.0 * spacing
    origin = target_center - half
    gdim = int(np.ceil(2.0 * half / spacing)) + 1
    shape = (gdim, gdim, gdim)
    inv = 1.0 / spacing

    rmax = float(radii.max()) * vdw_scale
    lo = origin - (rmax + spacing)
    hi = origin + 2.0 * half + (rmax + spacing)
    in_box = np.all((coords >= lo) & (coords <= hi), axis=1)
    coords_b = coords[in_box]
    radii_b = radii[in_box] * vdw_scale

    # vdW occupancy
    occupied = np.zeros(shape, dtype=bool)
    for (ax, ay, az), r in zip(coords_b, radii_b):
        r2 = r * r
        i0 = max(0, int(np.floor((ax - r - origin[0]) * inv)))
        i1 = min(shape[0] - 1, int(np.ceil((ax + r - origin[0]) * inv)))
        j0 = max(0, int(np.floor((ay - r - origin[1]) * inv)))
        j1 = min(shape[1] - 1, int(np.ceil((ay + r - origin[1]) * inv)))
        k0 = max(0, int(np.floor((az - r - origin[2]) * inv)))
        k1 = min(shape[2] - 1, int(np.ceil((az + r - origin[2]) * inv)))
        if i0 > i1 or j0 > j1 or k0 > k1:
            continue
        gx = origin[0] + np.arange(i0, i1 + 1) * spacing - ax
        gy = origin[1] + np.arange(j0, j1 + 1) * spacing - ay
        gz = origin[2] + np.arange(k0, k1 + 1) * spacing - az
        d2 = gx[:, None, None] ** 2 + gy[None, :, None] ** 2 + gz[None, None, :] ** 2
        occupied[i0:i1 + 1, j0:j1 + 1, k0:k1 + 1] |= (d2 <= r2)

    if not occupied.any():
        raise PocketError("no receptor atoms near the target; raise search_radius.")

    # solvent accessibility (excludes internal voids)
    ball = _ball(max(1.0, probe / spacing), np)
    occ_probe = ndimage.binary_dilation(occupied, structure=ball)
    lab, _ = ndimage.label(~occ_probe)
    faces = np.unique(np.concatenate([
        lab[0].ravel(), lab[-1].ravel(),
        lab[:, 0].ravel(), lab[:, -1].ravel(),
        lab[:, :, 0].ravel(), lab[:, :, -1].ravel()]))
    faces = faces[faces > 0]
    if faces.size == 0:
        raise PocketError("grid does not touch solvent; raise search_radius.")
    accessible = ndimage.binary_dilation(np.isin(lab, faces), structure=ball) & (~occupied)

    # LIGSITE(7) burial
    dirs = [[1, 0, 0], [0, 1, 0], [0, 0, 1],
            [1, 1, 1], [1, 1, -1], [1, -1, 1], [-1, 1, 1]]
    psp = np.zeros(shape, dtype=np.int8)
    for d in dirs:
        step = spacing * np.sqrt(sum(v * v for v in d))
        ms = max(1, int(round(max_burial / step)))
        psp += (_prot_within(occupied, d, ms, np) &
                _prot_within(occupied, [-x for x in d], ms, np)).astype(np.int8)

    pocket = accessible & (psp >= psp_min)
    fallback = False
    if not pocket.any():
        pocket = accessible  # nothing met psp_min -> use accessible surface
        fallback = True
    if not pocket.any():
        raise PocketError("no accessible pocket near the target.")

    # snap to the nearest pocket voxel to the target
    tidx = np.clip(np.round((target_center - origin) * inv).astype(int), 0, np.array(shape) - 1)
    cidx = np.argwhere(pocket)
    dtarget = np.linalg.norm((cidx - tidx) * spacing, axis=1)
    within = dtarget <= search_radius
    if not within.any():
        raise PocketError(f"no pocket within search_radius ({search_radius:.1f} A).")
    cidx = cidx[within]; dtarget = dtarget[within]
    seed = cidx[dtarget.argmin()]
    seed_world = origin + seed * spacing
    moved = float(np.linalg.norm(seed_world - target_center))

    # connected pocket component containing the snap
    labels, _ = ndimage.label(pocket, structure=np.ones((3, 3, 3), dtype=int))
    comp = np.argwhere(labels == labels[tuple(seed)])
    dcomp = np.linalg.norm((comp - seed) * spacing, axis=1)

    # CENTER = centroid of the local cavity volume (robust; independent of spacing/n_points)
    cmask = dcomp <= center_radius
    box_center = (origin + comp[cmask] * spacing).mean(axis=0) if cmask.any() else seed_world

    # BLOB = cavity within blob_radius, evenly spread by farthest-point sampling
    bregion = comp[dcomp <= blob_radius]
    start = int(np.argmin(np.linalg.norm((bregion - seed), axis=1)))
    blob = bregion[_fps(bregion.astype(float), start, n_points, np)]
    blob_world = origin + blob * spacing

    # SIZE from the cavity extent (box fits the pocket), clamped to a sensible range.
    span = float((blob_world.max(axis=0) - blob_world.min(axis=0)).max()) if len(blob_world) else 0.0
    edge = max(float(min_edge), min(float(max_edge), span + 2.0 * float(padding)))

    return {
        "center": (float(box_center[0]), float(box_center[1]), float(box_center[2])),
        "size": (edge, edge, edge),
        "points": [(float(p[0]), float(p[1]), float(p[2])) for p in blob_world],
        "moved": moved,
        "fallback": fallback,
    }


__all__ = ["pseudo_pocket_box", "PocketError"]


if __name__ == "__main__":
    # ponytail: one runnable smoke check on the full pipeline (voxelize -> accessibility ->
    # burial -> snap -> center/blob). A solid slab of atoms with the target just off one face:
    # the snap must land on the accessible surface near the target and return a usable box.
    import numpy as np

    slab = [
        (x, y, z)
        for x in range(-8, 9, 2)
        for y in range(-8, 9, 2)
        for z in range(-8, 1, 2)  # occupies z<=0; z>0 is solvent
    ]
    vdw = [1.7] * len(slab)
    target = [(0.0, 0.0, 3.0)]  # just above the slab surface
    box = pseudo_pocket_box(slab, vdw, target, search_radius=10.0, psp_min=1, blob_radius=8.0, n_points=40)
    cx, cy, cz = box["center"]
    assert box["moved"] < 10.0, box["moved"]              # snapped within search_radius
    assert -6.0 < cx < 6.0 and -6.0 < cy < 6.0, box["center"]  # near the target laterally
    assert len(box["points"]) > 0, "blob must have points"
    assert 18.0 <= box["size"][0] <= 30.0, box["size"]    # clamped edge
    print("pseudo_pocket_box OK center=", tuple(round(v, 2) for v in box["center"]),
          "size=", round(box["size"][0], 1), "blob=", len(box["points"]))
