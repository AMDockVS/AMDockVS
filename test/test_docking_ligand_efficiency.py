from pathlib import Path

from amdockvs.docking.engines import _count_heavy_atoms_pdbqt, _ligand_efficiency


def test_count_heavy_atoms_skips_hydrogens(tmp_path: Path) -> None:
    pdbqt = tmp_path / "lig.pdbqt"
    pdbqt.write_text(
        "REMARK something\n"
        "ROOT\n"
        "ATOM      1  C   LIG A   1       0.000   0.000   0.000  0.00  0.00     0.000 C\n"
        "ATOM      2  N   LIG A   1       1.000   0.000   0.000  0.00  0.00     0.000 NA\n"
        "ATOM      3  O   LIG A   1       2.000   0.000   0.000  0.00  0.00     0.000 OA\n"
        "ATOM      4  H   LIG A   1       3.000   0.000   0.000  0.00  0.00     0.000 HD\n"
        "HETATM    5 CL   LIG A   1       4.000   0.000   0.000  0.00  0.00     0.000 Cl\n"
        "ENDROOT\n"
        "TORSDOF 0\n",
        encoding="utf-8",
    )
    # C, NA, OA, Cl are heavy; HD is hydrogen.
    assert _count_heavy_atoms_pdbqt(pdbqt) == 4


def test_ligand_efficiency() -> None:
    assert _ligand_efficiency(-9.6, 24) == -0.4
    assert _ligand_efficiency(-9.6, 0) is None
