from __future__ import annotations


def align_molecule_in_place(
    probe_mol,
    ref_mol,
    *,
    probe_conf_id: int = -1,
    ref_conf_id: int = -1,
    atom_map: list[tuple[int, int]] | None = None,
    reflect: bool = False,
    max_iters: int = 50,
) -> float:
    from rdkit.Chem import rdMolAlign

    kwargs = {
        "prbCid": int(probe_conf_id),
        "refCid": int(ref_conf_id),
        "reflect": bool(reflect),
        "maxIters": int(max_iters),
    }
    if atom_map is not None:
        kwargs["atomMap"] = atom_map
    return float(rdMolAlign.AlignMol(probe_mol, ref_mol, **kwargs))


def aligned_copy(
    probe_mol,
    ref_mol,
    *,
    probe_conf_id: int = -1,
    ref_conf_id: int = -1,
    atom_map: list[tuple[int, int]] | None = None,
    reflect: bool = False,
    max_iters: int = 50,
):
    from rdkit import Chem

    aligned_mol = Chem.Mol(probe_mol)
    rmsd = align_molecule_in_place(
        aligned_mol,
        ref_mol,
        probe_conf_id=probe_conf_id,
        ref_conf_id=ref_conf_id,
        atom_map=atom_map,
        reflect=reflect,
        max_iters=max_iters,
    )
    return aligned_mol, rmsd


__all__ = ["align_molecule_in_place", "aligned_copy"]
