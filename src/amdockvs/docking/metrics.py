from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any


R_KCAL_MOL_K = 0.00198720425864083
DEFAULT_TEMPERATURE_K = 298.15


def predicted_ki_m(delta_g_kcal_mol: float, *, temperature_k: float = DEFAULT_TEMPERATURE_K) -> float | None:
    """Estimate Ki from docking delta G: dG = RT ln(Ki)."""
    try:
        exponent = float(delta_g_kcal_mol) / (R_KCAL_MOL_K * float(temperature_k))
        return float(math.exp(exponent))
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def predicted_pki(delta_g_kcal_mol: float, *, temperature_k: float = DEFAULT_TEMPERATURE_K) -> float | None:
    ki = predicted_ki_m(delta_g_kcal_mol, temperature_k=temperature_k)
    if ki is None or ki <= 0.0:
        return None
    return -math.log10(ki)


def ligand_efficiency(score: float, heavy_atoms: int) -> float | None:
    return round(float(score) / int(heavy_atoms), 4) if int(heavy_atoms or 0) > 0 else None


def _fit_quality_scale(heavy_atoms: int) -> float | None:
    """Hopkins-style LE scale approximation used for fit quality normalization."""
    n = int(heavy_atoms or 0)
    if n <= 0:
        return None
    return 0.0715 + (7.5328 / n) + (25.7079 / (n**2)) - (361.4722 / (n**3))


def _load_rdkit_mol(path: Path):
    try:
        from rdkit import Chem
    except ImportError:
        return None
    suffix = path.suffix.lower()
    if suffix in {".sdf", ".sd", ".mol"}:
        supplier = Chem.SDMolSupplier(str(path), sanitize=True, removeHs=False)
        return supplier[0] if supplier and len(supplier) > 0 else None
    if suffix == ".mol2":
        return Chem.MolFromMol2File(str(path), sanitize=True, removeHs=False)
    if suffix in {".pdb", ".ent"}:
        return Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=False)
    return None


def ligand_descriptors(path: str | Path | None, *, heavy_atoms_fallback: int = 0) -> dict[str, Any]:
    descriptors: dict[str, Any] = {}
    if path is not None:
        source = Path(path).expanduser()
        if source.exists():
            mol = _load_rdkit_mol(source.resolve())
            if mol is not None:
                try:
                    from rdkit.Chem import Crippen, Descriptors, Lipinski, rdMolDescriptors

                    descriptors.update(
                        {
                            "heavy_atoms": int(mol.GetNumHeavyAtoms()),
                            "molecular_weight": float(Descriptors.MolWt(mol)),
                            "tpsa": float(rdMolDescriptors.CalcTPSA(mol)),
                            "clogp": float(Crippen.MolLogP(mol)),
                            "hbd": int(Lipinski.NumHDonors(mol)),
                            "hba": int(Lipinski.NumHAcceptors(mol)),
                        }
                    )
                except Exception:
                    pass
    if int(descriptors.get("heavy_atoms") or 0) <= 0 and int(heavy_atoms_fallback or 0) > 0:
        descriptors["heavy_atoms"] = int(heavy_atoms_fallback)
    return descriptors


def docking_metrics(
    *,
    score: float,
    ligand_source_path: str | Path | None = None,
    heavy_atoms_fallback: int = 0,
    descriptors: Mapping[str, Any] | None = None,
    temperature_k: float = DEFAULT_TEMPERATURE_K,
) -> dict[str, Any]:
    # The descriptors the ligand already carries in the DB (mw/logp/tpsa) beat re-parsing its
    # file: the worker has no project root to resolve a relative path with, and that silently
    # left clogp empty — no LLE, no BEI, no SEI. Reading the file stays as the fallback.
    desc = {key: value for key, value in dict(descriptors or {}).items() if value is not None}
    if "clogp" not in desc or not desc.get("heavy_atoms"):
        desc = {**ligand_descriptors(ligand_source_path, heavy_atoms_fallback=heavy_atoms_fallback), **desc}
    if int(desc.get("heavy_atoms") or 0) <= 0 and int(heavy_atoms_fallback or 0) > 0:
        desc["heavy_atoms"] = int(heavy_atoms_fallback)
    heavy_atoms = int(desc.get("heavy_atoms") or heavy_atoms_fallback or 0)
    pki = predicted_pki(score, temperature_k=temperature_k)
    ki = predicted_ki_m(score, temperature_k=temperature_k)
    metrics: dict[str, Any] = {
        "heavy_atoms": heavy_atoms,
        "ligand_efficiency": ligand_efficiency(score, heavy_atoms),
        "predicted_ki_m": None if ki is None else float(ki),
        "predicted_pki": None if pki is None else round(float(pki), 4),
    }
    for key in ("molecular_weight", "tpsa", "clogp", "hbd", "hba"):
        if key in desc:
            metrics[key] = desc[key]
    if pki is not None:
        mw = float(desc.get("molecular_weight") or 0.0)
        tpsa = float(desc.get("tpsa") or 0.0)
        clogp = desc.get("clogp")
        if clogp is not None:
            metrics["lipophilic_efficiency"] = round(float(pki) - float(clogp), 4)
            metrics["lle"] = metrics["lipophilic_efficiency"]
        if mw > 0.0:
            metrics["bei"] = round(float(pki) * 1000.0 / mw, 4)
        if tpsa > 0.0:
            metrics["sei"] = round(float(pki) * 100.0 / tpsa, 4)
    le = metrics.get("ligand_efficiency")
    scale = _fit_quality_scale(heavy_atoms)
    if le is not None and scale and scale > 0.0:
        metrics["fit_quality"] = round(abs(float(le)) / scale, 4)
    return {key: value for key, value in metrics.items() if value is not None}


__all__ = [
    "docking_metrics",
    "ligand_descriptors",
    "ligand_efficiency",
    "predicted_ki_m",
    "predicted_pki",
]
