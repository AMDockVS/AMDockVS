"""AutoDock4 docking engine: composed autogrid4 (maps per receptor) + autodock4 (per ligand).

Reuses the Vina PDBQT preparation (same receptor/ligand .pdbqt). This is the example
of a COMPOSED engine on the DOCK_RUNNERS seam; intended for testing the multi-program
path (Vina already covers production scoring). Pose output is the best DOCKED pdbqt.

ponytail: minimal GPF/DPF written by hand with AutoDockTools default parameters; no
AD4_parameters.dat (uses autogrid built-in types). Upgrade path if real AD4 runs are
needed: prepare_gpf4/prepare_dpf4 from AutoDockTools, sdf pose conversion, clustering.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from amdockvs.docking.metrics import docking_metrics
from amdockvs.molecule_paths import get_default_project_root


def _binary(env_var: str, name: str) -> str:
    # $OVERRIDE, else PATH (e.g. `conda install -c conda-forge autogrid`), else the bare name so
    # the missing-binary error names the tool instead of a machine-specific path.
    return os.environ.get(env_var) or shutil.which(name) or name


AUTOGRID4 = _binary("AMDOCK_AUTOGRID4", "autogrid4")
AUTODOCK4 = _binary("AMDOCK_AUTODOCK4", "autodock4")


def _atom_types(pdbqt: Path) -> list[str]:
    types: list[str] = []
    for line in pdbqt.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(("ATOM", "HETATM")):
            t = line[77:79].strip()
            if t and t not in types:
                types.append(t)
    return types


def _ligand_center_and_torsdof(pdbqt: Path) -> tuple[tuple[float, float, float], int]:
    xs, ys, zs = [], [], []
    torsdof = 0
    for line in pdbqt.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(("ATOM", "HETATM")):
            xs.append(float(line[30:38])); ys.append(float(line[38:46])); zs.append(float(line[46:54]))
        elif line.startswith("TORSDOF"):
            torsdof = int(line.split()[1])
    n = max(1, len(xs))
    return (sum(xs) / n, sum(ys) / n, sum(zs) / n), torsdof


def _npts(size: float, spacing: float) -> int:
    n = int(round(float(size) / float(spacing)))
    n -= n % 2  # autogrid wants even npts
    return max(2, min(126, n))


def _run(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}")


def _parse_dlg_energies(dlg_text: str) -> list[float]:
    """Binding energies (kcal/mol) of DOCKED models, ascending (best first)."""
    energies = [
        float(m)
        for m in re.findall(
            r"DOCKED:\s*USER\s+Estimated Free Energy of Binding\s*=\s*([-+]?\d+\.\d+)", dlg_text
        )
    ]
    return sorted(energies)


def _best_pose_pdbqt(dlg_text: str) -> str:
    """First DOCKED MODEL block as a plain pdbqt (strip the 'DOCKED: ' prefix)."""
    lines = dlg_text.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if not line.startswith("DOCKED:"):
            continue
        body = line[len("DOCKED:"):].lstrip()
        if body.startswith("MODEL"):
            capturing = True
            out = [body]
            continue
        if capturing:
            out.append(body)
            if body.startswith("ENDMDL"):
                break
    return "\n".join(out) + "\n" if out else ""


def _count_heavy_atoms_pdbqt(path: Path) -> int:
    count = 0
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw.startswith(("ATOM", "HETATM")):
            continue
        ad_type = raw.split()[-1] if raw.split() else ""
        if ad_type and not ad_type.upper().startswith("H"):
            count += 1
    return count


def _resolve_optional_path(value: object, project_root: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    return path.resolve()


def _autogrid(receptor_pdbqt: Path, ligand_types: list[str], center, size, spacing: float, work: Path) -> Path:
    """Write GPF, run autogrid4, return the .maps.fld path."""
    stem = receptor_pdbqt.stem
    local_receptor = work / receptor_pdbqt.name
    if not local_receptor.exists():
        shutil.copy2(receptor_pdbqt, local_receptor)
    rtypes = _atom_types(receptor_pdbqt)
    npts = [_npts(size[i], spacing) for i in range(3)]
    gpf = work / f"{stem}.gpf"
    maps = [f"map {stem}.{t}.map" for t in ligand_types]
    gpf.write_text(
        "\n".join(
            [
                f"npts {npts[0]} {npts[1]} {npts[2]}",
                f"gridfld {stem}.maps.fld",
                f"spacing {spacing}",
                f"receptor_types {' '.join(rtypes)}",
                f"ligand_types {' '.join(ligand_types)}",
                f"receptor {local_receptor.name}",
                f"gridcenter {center[0]:.3f} {center[1]:.3f} {center[2]:.3f}",
                "smooth 0.5",
                *maps,
                f"elecmap {stem}.e.map",
                f"dsolvmap {stem}.d.map",
                "dielectric -0.1465",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run([AUTOGRID4, "-p", gpf.name, "-l", f"{stem}.glg"], cwd=work)
    return work / f"{stem}.maps.fld"


def _autodock(ligand_pdbqt: Path, receptor_stem: str, ligand_types: list[str], num_modes: int, work: Path) -> str:
    """Write DPF, run autodock4 for one ligand, return the .dlg text."""
    local_ligand = work / ligand_pdbqt.name
    if not local_ligand.exists():
        shutil.copy2(ligand_pdbqt, local_ligand)
    center, torsdof = _ligand_center_and_torsdof(ligand_pdbqt)
    stem = ligand_pdbqt.stem
    dpf = work / f"{stem}.dpf"
    maps = [f"map {receptor_stem}.{t}.map" for t in ligand_types]
    dpf.write_text(
        "\n".join(
            [
                "autodock_parameter_version 4.2",
                "outlev 1",
                "intelec",
                "seed pid time",
                f"ligand_types {' '.join(ligand_types)}",
                f"fld {receptor_stem}.maps.fld",
                *maps,
                f"elecmap {receptor_stem}.e.map",
                f"desolvmap {receptor_stem}.d.map",
                f"move {local_ligand.name}",
                f"about {center[0]:.3f} {center[1]:.3f} {center[2]:.3f}",
                "tran0 random",
                "quaternion0 random",
                "dihe0 random",
                f"torsdof {torsdof}",
                "rmstol 2.0",
                "extnrg 1000.0",
                "e0max 0.0 10000",
                "ga_pop_size 150",
                "ga_num_evals 2500000",
                "ga_num_generations 27000",
                "ga_elitism 1",
                "ga_mutation_rate 0.02",
                "ga_crossover_rate 0.8",
                "ga_window_size 10",
                "ga_cauchy_alpha 0.0",
                "ga_cauchy_beta 1.0",
                "set_ga",
                "sw_max_its 300",
                "sw_max_succ 4",
                "sw_max_fail 4",
                "sw_rho 1.0",
                "sw_lb_rho 0.01",
                "ls_search_freq 0.06",
                "set_psw1",
                "unbound_model bound",
                f"ga_run {max(1, int(num_modes))}",
                "analysis",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run([AUTODOCK4, "-p", dpf.name, "-l", f"{stem}.dlg"], cwd=work)
    return (work / f"{stem}.dlg").read_text(encoding="utf-8", errors="ignore")


def autodock4_dock_runner(payload: dict) -> list[dict]:
    pairs = list(payload.get("pairs") or [])
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    default_center = [float(v) for v in (payload.get("box_center") or [])]
    default_size = [float(v) for v in (payload.get("box_size") or [])]
    default_spacing = float(payload.get("spacing") or 0.375)
    run_id = str(payload.get("run_id") or "")
    project_root = get_default_project_root()

    # Group by receptor so autogrid runs once per receptor (maps are receptor-specific).
    by_receptor: dict[str, list[dict]] = {}
    for pair in pairs:
        by_receptor.setdefault(str(pair.get("receptor_path") or ""), []).append(pair)

    rows: list[dict] = []
    for receptor_path_text, group in by_receptor.items():
        receptor_path = Path(receptor_path_text).expanduser().resolve()
        work = output_dir / f"ad4_{receptor_path.stem}"
        work.mkdir(parents=True, exist_ok=True)
        center = [float(v) for v in (group[0].get("box_center") or default_center)]
        size = [float(v) for v in (group[0].get("box_size") or default_size)]
        spacing = float(group[0].get("spacing") or default_spacing)
        # Union of ligand atom types across the group -> maps to compute.
        ligand_types: list[str] = []
        for pair in group:
            for t in _atom_types(Path(str(pair.get("ligand_path"))).expanduser().resolve()):
                if t not in ligand_types:
                    ligand_types.append(t)
        _autogrid(receptor_path, ligand_types, center, size, spacing, work)

        for pair in group:
            ligand_path = Path(str(pair.get("ligand_path"))).expanduser().resolve()
            num_modes = int(pair.get("num_modes") or 9)
            dlg = _autodock(ligand_path, receptor_path.stem, ligand_types, num_modes, work)
            energies = _parse_dlg_energies(dlg)
            pose_path = output_dir / f"{int(pair.get('ligand_id') or 0)}__{int(pair.get('receptor_id') or 0)}.ad4.pdbqt"
            pose_path.write_text(_best_pose_pdbqt(dlg), encoding="utf-8")
            pose_text = str(pose_path)
            if project_root is not None:
                with suppress(Exception):
                    pose_text = str(pose_path.relative_to(project_root))
            ligand_source_path = _resolve_optional_path(pair.get("ligand_source_path"), project_root)
            heavy_atoms = _count_heavy_atoms_pdbqt(ligand_path)
            for rank, score in enumerate(energies, start=1):
                metrics = docking_metrics(
                    score=float(score),
                    ligand_source_path=ligand_source_path,
                    heavy_atoms_fallback=heavy_atoms,
                    descriptors=pair.get("ligand_descriptors"),
                )
                rows.append(
                    {
                        "receptor_molecule_id": int(pair.get("receptor_id") or 0),
                        "ligand_molecule_id": int(pair.get("ligand_id") or 0),
                        "engine": "autodock4",
                        "pose_rank": rank,
                        "score": float(score),
                        "score_type": "ad4_score",
                        "pose_path": pose_text,
                        "rmsd_vs_reference": None,
                        "metrics": {
                            "engine": "autodock4",
                            "run_kind": str(pair.get("run_kind") or "screening"),
                            "ligand_id": int(pair.get("ligand_id") or 0),
                            "receptor_id": int(pair.get("receptor_id") or 0),
                            "ligand_path": str(pair.get("ligand_path_logical") or ligand_path),
                            "ligand_source_path": str(
                                pair.get("ligand_source_path_logical") or pair.get("ligand_source_path") or ""
                            ),
                            "receptor_path": str(pair.get("receptor_path_logical") or receptor_path),
                            "grid": {"box_center": center, "box_size": size, "spacing": spacing},
                            "energy_kcal_mol": float(score),
                            **metrics,
                            "is_selected": rank == 1,
                            "run_id": run_id,
                            "generated_at": datetime.now().isoformat(),
                        },
                        "created_at": datetime.now(),
                    }
                )
    return rows


from amdockvs.docking.engines import register_dock_runner  # noqa: E402

register_dock_runner("autodock4", autodock4_dock_runner)


if __name__ == "__main__":
    sample = (
        "DOCKED: MODEL        1\n"
        "DOCKED: USER    Estimated Free Energy of Binding    =   -8.50 kcal/mol\n"
        "DOCKED: ATOM      1  C   LIG     1       1.000   2.000   3.000  0.00  0.00     0.000 C\n"
        "DOCKED: ENDMDL\n"
        "DOCKED: USER    Estimated Free Energy of Binding    =   -9.10 kcal/mol\n"
    )
    assert _parse_dlg_energies(sample) == [-9.10, -8.50], _parse_dlg_energies(sample)
    pose = _best_pose_pdbqt(sample)
    assert pose.startswith("MODEL") and "ATOM" in pose and pose.endswith("ENDMDL\n"), repr(pose)
    print("autodock4 parser self-check OK")
