from __future__ import annotations

from typing import Literal

FingerprintKind = Literal["morgan", "rdkit"]


def morgan_fingerprint(
    mol,
    *,
    radius: int = 2,
    n_bits: int = 2048,
    use_chirality: bool = True,
):
    from rdkit.Chem import rdFingerprintGenerator

    generator = rdFingerprintGenerator.GetMorganGenerator(
        radius=int(radius),
        fpSize=int(n_bits),
        includeChirality=bool(use_chirality),
    )
    return generator.GetFingerprint(mol)


def rdkit_fingerprint(
    mol,
    *,
    fp_size: int = 2048,
    min_path: int = 1,
    max_path: int = 7,
):
    from rdkit import Chem

    return Chem.RDKFingerprint(
        mol,
        fpSize=int(fp_size),
        minPath=int(min_path),
        maxPath=int(max_path),
    )


def fingerprint_from_molecule(
    mol,
    *,
    kind: FingerprintKind = "morgan",
    **kwargs,
):
    if kind == "morgan":
        return morgan_fingerprint(mol, **kwargs)
    if kind == "rdkit":
        return rdkit_fingerprint(mol, **kwargs)
    raise ValueError(f"Unsupported fingerprint kind: {kind}")


def fingerprint_to_bitstring(fp) -> str:
    return fp.ToBitString()


def tanimoto_similarity(fp_a, fp_b) -> float:
    from rdkit import DataStructs

    return float(DataStructs.TanimotoSimilarity(fp_a, fp_b))


__all__ = [
    "FingerprintKind",
    "fingerprint_from_molecule",
    "fingerprint_to_bitstring",
    "morgan_fingerprint",
    "rdkit_fingerprint",
    "tanimoto_similarity",
]
