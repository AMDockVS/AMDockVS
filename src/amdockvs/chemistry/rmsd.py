from __future__ import annotations


def best_rmsd(
    probe_mol,
    ref_mol,
    *,
    probe_conf_id: int = -1,
    ref_conf_id: int = -1,
) -> float:
    from rdkit.Chem import rdMolAlign

    return float(
        rdMolAlign.GetBestRMS(
            probe_mol,
            ref_mol,
            prbId=int(probe_conf_id),
            refId=int(ref_conf_id),
        )
    )


def conformer_rmsd_matrix(mol) -> list[list[float]]:
    from rdkit.Chem import AllChem

    flat_values = list(AllChem.GetConformerRMSMatrix(mol, prealigned=False))
    n_conformers = mol.GetNumConformers()
    matrix = [[0.0 for _ in range(n_conformers)] for _ in range(n_conformers)]
    cursor = 0
    for row in range(1, n_conformers):
        for col in range(row):
            value = float(flat_values[cursor])
            matrix[row][col] = value
            matrix[col][row] = value
            cursor += 1
    return matrix


__all__ = ["best_rmsd", "conformer_rmsd_matrix"]
