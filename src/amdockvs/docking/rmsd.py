from __future__ import annotations

import math
from contextlib import suppress
from pathlib import Path

from rdkit import Chem


def _first_mol(path: Path, *, pose_rank: int = 1):
    suffix = path.suffix.lower()
    if suffix in {".sdf", ".sd"}:
        supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False)
        index = max(0, int(pose_rank or 1) - 1)
        if supplier is None or len(supplier) <= index:
            return None
        return supplier[index]
    if suffix == ".mol2":
        return Chem.MolFromMol2File(str(path), sanitize=False, removeHs=False)
    if suffix == ".pdb":
        return Chem.MolFromPDBFile(str(path), sanitize=False, removeHs=False)
    if suffix == ".mol":
        return Chem.MolFromMolFile(str(path), sanitize=False, removeHs=False)
    return None


def _heavy(mol):
    if mol is None:
        return None
    with suppress(Exception):
        return Chem.RemoveHs(mol, sanitize=False)
    return mol


def _ordered_rmsd(reference, pose) -> float | None:
    if reference is None or pose is None:
        return None
    if reference.GetNumAtoms() != pose.GetNumAtoms():
        return None
    if reference.GetNumConformers() < 1 or pose.GetNumConformers() < 1:
        return None
    ref_conf = reference.GetConformer()
    pose_conf = pose.GetConformer()
    total = 0.0
    count = int(reference.GetNumAtoms())
    for index in range(count):
        a = ref_conf.GetAtomPosition(index)
        b = pose_conf.GetAtomPosition(index)
        total += (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
    return math.sqrt(total / max(1, count))


def _mapped_rmsd_no_align(reference, pose) -> tuple[float, str] | None:
    """Return (rmsd, method) with method in {"substruct", "ordered"}.

    "substruct" is the symmetry-aware match; "ordered" is the MGLTools-style 1:1-by-index
    fallback, only valid when reference and pose share atom ordering — callers surface the
    method so the fragile "ordered" cases can be identified and audited.
    """
    if reference is None or pose is None:
        return None
    if reference.GetNumAtoms() != pose.GetNumAtoms():
        return None
    if reference.GetNumConformers() < 1 or pose.GetNumConformers() < 1:
        return None
    ref_conf = reference.GetConformer()
    pose_conf = pose.GetConformer()
    count = int(reference.GetNumAtoms())
    mappings = ()
    with suppress(Exception):
        mappings = pose.GetSubstructMatches(reference, uniquify=False, maxMatches=1000)
    best: float | None = None
    for mapping in mappings:
        if len(mapping) != count:
            continue
        total = 0.0
        for ref_index, pose_index in enumerate(mapping):
            a = ref_conf.GetAtomPosition(ref_index)
            b = pose_conf.GetAtomPosition(int(pose_index))
            total += (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
        rmsd = math.sqrt(total / max(1, count))
        best = rmsd if best is None else min(best, rmsd)
    if best is not None:
        return best, "substruct"
    ordered = _ordered_rmsd(reference, pose)
    return None if ordered is None else (ordered, "ordered")


def pose_rmsd_detail(
    *,
    reference_ligand_path: str | Path | None,
    pose_path: str | Path | None,
    pose_rank: int = 1,
) -> tuple[float, str] | None:
    """Heavy-atom RMSD (angstroms) plus the match method used, or None if uncomputable."""
    ref_text = str(reference_ligand_path or "").strip()
    pose_text = str(pose_path or "").strip()
    if not ref_text or not pose_text:
        return None
    ref_path = Path(ref_text).expanduser()
    docked_path = Path(pose_text).expanduser()
    if not ref_path.exists() or not docked_path.exists():
        return None
    reference = _heavy(_first_mol(ref_path, pose_rank=1))
    pose = _heavy(_first_mol(docked_path, pose_rank=pose_rank))
    if reference is None or pose is None:
        return None
    return _mapped_rmsd_no_align(reference, pose)


def pose_rmsd_vs_reference(
    *,
    reference_ligand_path: str | Path | None,
    pose_path: str | Path | None,
    pose_rank: int = 1,
) -> float | None:
    """Return heavy-atom RMSD in angstroms for a docked pose vs its reference ligand."""
    detail = pose_rmsd_detail(
        reference_ligand_path=reference_ligand_path,
        pose_path=pose_path,
        pose_rank=pose_rank,
    )
    return None if detail is None else detail[0]


__all__ = ["pose_rmsd_detail", "pose_rmsd_vs_reference"]
