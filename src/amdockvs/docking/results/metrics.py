from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any


from amdockvs.core.configuration import DEFAULT_TEMPERATURE_K

R_KCAL_MOL_K = 0.00198720425864083


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
    from amdockvs.io.formats import read_mol

    return read_mol(path)


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


def pose_file_stem(pair: Mapping[str, Any], protocol: Mapping[str, Any] | None) -> str:
    """The name an engine gives the files of one docking, before its own ending: the pair (its complex
    in a redocking) and the protocol, so two protocols of a pair do not write over each other's poses."""
    complex_id = int(pair.get("complex_id") or 0)
    stem = (f"{pair.get('run_kind') or 'screening'}_{complex_id}" if complex_id
            else f"{int(pair.get('ligand_id') or 0)}__{int(pair.get('receptor_id') or 0)}")
    digest = str((protocol or {}).get("hash") or "").strip()
    return f"{stem}__proto_{digest[:12]}" if digest else stem


# What `docking_metrics` derives from a score: columns of `docking_results`.
SCORE_METRICS = ("ligand_efficiency", "predicted_pki", "lle", "bei", "sei", "fit_quality")


def docking_metrics(
    *,
    score: float,
    ligand_source_path: str | Path | None = None,
    heavy_atoms_fallback: int = 0,
    descriptors: Mapping[str, Any] | None = None,
    temperature_k: float = DEFAULT_TEMPERATURE_K,
) -> dict[str, float]:
    """The `SCORE_METRICS` of one pose that can be computed; the ligand's own properties stay in
    `molecules`."""
    # The descriptors the ligand already carries in the DB (mw/logp/tpsa) beat re-parsing its
    # file: the worker has no project root to resolve a relative path with, and that silently
    # left clogp empty — no LLE, no BEI, no SEI. Reading the file stays as the fallback.
    desc = {key: value for key, value in dict(descriptors or {}).items() if value is not None}
    if "clogp" not in desc or not desc.get("heavy_atoms"):
        desc = {**ligand_descriptors(ligand_source_path, heavy_atoms_fallback=heavy_atoms_fallback), **desc}
    heavy_atoms = int(desc.get("heavy_atoms") or heavy_atoms_fallback or 0)
    pki = predicted_pki(score, temperature_k=temperature_k)
    metrics: dict[str, Any] = {
        "ligand_efficiency": ligand_efficiency(score, heavy_atoms),
        "predicted_pki": None if pki is None else round(float(pki), 4),
    }
    if pki is not None:
        mw = float(desc.get("molecular_weight") or 0.0)
        tpsa = float(desc.get("tpsa") or 0.0)
        clogp = desc.get("clogp")
        if clogp is not None:
            metrics["lle"] = round(float(pki) - float(clogp), 4)
        if mw > 0.0:
            metrics["bei"] = round(float(pki) * 1000.0 / mw, 4)
        if tpsa > 0.0:
            metrics["sei"] = round(float(pki) * 100.0 / tpsa, 4)
    le = metrics.get("ligand_efficiency")
    scale = _fit_quality_scale(heavy_atoms)
    if le is not None and scale and scale > 0.0:
        metrics["fit_quality"] = round(abs(float(le)) / scale, 4)
    return {key: value for key, value in metrics.items() if value is not None}


def docking_row(
    pair: Mapping[str, Any],
    *,
    score_type: str,
    pose_rank: int = 1,
    score: float | None = None,
    pose_path: str = "",
    rmsd: tuple[float, str] | None = None,
    error: str = "",
    extra: Mapping[str, Any] | None = None,
    heavy_atoms: int = 0,
) -> dict[str, Any]:
    """One pose of a pair as every engine returns it: the columns of `docking_results` (the
    job adds `docking_run_id`), plus `pose_path`, the engine's file the pose is read from.
    A failed pair is `error` and no score."""
    extra = {key: value for key, value in dict(extra or {}).items() if value not in (None, "")}
    if rmsd is not None and rmsd[1]:
        extra["rmsd_method"] = rmsd[1]
    row = {
        "receptor_molecule_id": int(pair.get("receptor_id") or 0),
        "ligand_molecule_id": int(pair.get("ligand_id") or 0),
        "complex_id": int(pair.get("complex_id") or 0) or None,
        "pose_rank": int(pose_rank),
        "score": None if score is None else float(score),
        "score_type": score_type,
        "rmsd_vs_reference": None if rmsd is None else rmsd[0],
        "error": str(error) or None,
        "extra": extra or None,
        "pose_path": str(pose_path or ""),
        **dict.fromkeys(SCORE_METRICS),
    }
    if score is not None:
        row.update(docking_metrics(
            score=float(score),
            ligand_source_path=pair.get("ligand_source_path"),
            heavy_atoms_fallback=heavy_atoms,
            descriptors=pair.get("ligand_descriptors"),
        ))
    return row


__all__ = [
    "SCORE_METRICS",
    "docking_metrics",
    "docking_row",
    "ligand_descriptors",
    "ligand_efficiency",
    "predicted_ki_m",
    "predicted_pki",
]
