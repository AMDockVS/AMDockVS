"""A residue picked in the 2D diagram, or one contact picked in the interaction table, shown in
PyMOL with its contacts drawn in 3D.

Each contact is a PyMOL distance between two pseudoatoms, coloured like its line in the 2D
diagram, so both views read the same. Ring contacts start from the ring centroid (a small
sphere); water bridges go through the water. No atom-index mapping crosses into PyMOL:
the diagram's ligand atoms are the pose's heavy atoms in file order, which is also the
order PyMOL keeps, so the coordinates come straight from the loaded pose.
"""
from __future__ import annotations

GROUP = "amdock_interactions"


def residue_selection(receptor_obj: str, key: str) -> str:
    """``A:248B:LEU`` -> the PyMOL selection of that residue in ``receptor_obj``."""
    chain, number, _name = key.split(":")
    selection = f"{receptor_obj} and resi {number}"
    return f"{selection} and chain {chain}" if chain.strip() else selection


def _centroid(points) -> tuple[float, float, float]:
    n = len(points)
    return tuple(sum(p[i] for p in points) / n for i in range(3))


def _nearest(cmd, selection: str, to) -> tuple[float, float, float] | None:
    coords = cmd.get_coords(selection)
    if coords is None or not len(coords):
        return None
    return tuple(min(coords, key=lambda p: sum((p[i] - to[i]) ** 2 for i in range(3))))


def _nearest_pair(cmd, ligand, selection: str):
    """The ligand point closest to any atom of ``selection``, or ``None`` when it selects nothing."""
    import numpy as np

    target = cmd.get_coords(selection)
    if target is None or not len(target):
        return None
    gaps = np.linalg.norm(np.asarray(ligand)[:, None, :] - np.asarray(target)[None, :, :], axis=2)
    return tuple(ligand[int(gaps.min(axis=1).argmin())])


def show_residue_interactions(cmd, *, receptor_obj: str, ligand_obj: str, key: str, diagram) -> int:
    """Sticks + zoom on residue ``key``, one dashed distance per contact; the number drawn."""
    ligand = cmd.get_coords(f"{ligand_obj} and not elem H")
    if ligand is None or len(ligand) != diagram.mol.GetNumAtoms():
        ligand = None  # not the pose this diagram was built from: show the residue, draw nothing
    contacts = [c for c in diagram.interactions if c.residue_key == key or c.via_water == key]
    return _draw(cmd, receptor_obj, ligand_obj, key, contacts, ligand)


def show_interaction(cmd, *, receptor_obj: str, ligand_obj: str, contact) -> int:
    """One contact picked from the interaction table (``kind``, ``residue_key``, ``ligand_atoms``,
    ``protein_atom``, ``via_water`` attributes), drawn like the diagram draws it."""
    ligand = cmd.get_coords(f"{ligand_obj} and not elem H")
    return _draw(cmd, receptor_obj, ligand_obj, contact.residue_key, [contact], ligand)


def _draw(cmd, receptor_obj: str, ligand_obj: str, key: str, contacts, ligand) -> int:
    from ms_contactmap.model import INTERACTION_STYLES

    cmd.delete(GROUP)
    residue = residue_selection(receptor_obj, key)
    cmd.show("sticks", f"({residue}) and not hydro")
    cmd.label(f"({residue}) and name CA", '"%s %s%s" % (resn, chain, resi)')
    if ligand is None:
        cmd.zoom(residue, 5)
        return 0

    names: list[str] = []

    def point(xyz, *, sphere: bool = False) -> str:
        name = f"{GROUP}_p{len(names)}"
        cmd.pseudoatom(name, pos=[float(v) for v in xyz])
        cmd.hide("everything", name)
        if sphere:
            cmd.show("spheres", name)
            cmd.set("sphere_scale", 0.25, name)
        names.append(name)
        return name

    def dash(a: str, b: str, color: str) -> None:
        name = f"{GROUP}_d{len(names)}"
        cmd.distance(name, a, b)
        cmd.set("dash_color", color.replace("#", "0x"), name)
        cmd.set("dash_gap", 0.35, name)
        names.append(name)

    drawn = 0
    for interaction in contacts:
        protein = residue_selection(receptor_obj, interaction.residue_key)
        if interaction.protein_atom:
            protein += f" and name {interaction.protein_atom}"
        atoms = [ligand[i] for i in interaction.ligand_atoms if i < len(ligand)]
        if not atoms:  # a table row saved without its ligand atoms: the ligand atom nearest the residue
            near = _nearest_pair(cmd, ligand, protein) or _nearest_pair(cmd, ligand, residue_selection(receptor_obj, interaction.residue_key))
            if near is None:
                continue
            atoms = [near]
        start = _centroid(atoms)
        color = INTERACTION_STYLES[interaction.kind].color if interaction.kind in INTERACTION_STYLES else "#808080"
        a = point(start, sphere=len(atoms) > 1)
        if interaction.via_water:
            water = _nearest(cmd, residue_selection(receptor_obj, interaction.via_water), start)
            if water is not None:
                w = point(water, sphere=True)
                dash(a, w, color)
                a = w
        end = _nearest(cmd, protein, start) or _nearest(cmd, residue_selection(receptor_obj, interaction.residue_key), start)
        if end is None:
            continue
        dash(a, point(end), color)
        drawn += 1
    if names:
        cmd.group(GROUP, " ".join(names))
    cmd.zoom(f"({residue}) or {ligand_obj}", 3)
    return drawn
