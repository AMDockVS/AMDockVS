"""autogrid4 affinity maps for the AutoDock4 force field.

Shared by the two engines that score with it: Vina (scoring_function="ad4") and
AutoDock-GPU. Both read the maps instead of the receptor, so everything the receptor
contributes (and any custom parameter file) is baked in here.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

# $OVERRIDE, else PATH (e.g. `conda install -c conda-forge autogrid`), else the bare name so
# the missing-binary error names the tool instead of a machine-specific path.
AUTOGRID4 = os.environ.get("AMDOCK_AUTOGRID4") or shutil.which("autogrid4") or "autogrid4"


def atom_types(pdbqt: Path) -> list[str]:
    types: list[str] = []
    for line in pdbqt.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(("ATOM", "HETATM")):
            t = line[77:79].strip()
            if t and t not in types:
                types.append(t)
    return types


def _npts(size: float, spacing: float) -> int:
    n = int(round(float(size) / float(spacing)))
    n -= n % 2  # autogrid wants even npts
    return max(2, min(126, n))


def autogrid(
    receptor_pdbqt: Path,
    ligand_types: list[str],
    center,
    size,
    spacing: float,
    work: Path,
    parameter_file: Path | None = None,
) -> Path:
    """Write GPF, run autogrid4, return the .maps.fld path."""
    stem = receptor_pdbqt.stem
    local_receptor = work / receptor_pdbqt.name
    if not local_receptor.exists():
        shutil.copy2(receptor_pdbqt, local_receptor)
    header: list[str] = []
    if parameter_file is not None:
        # Must be the first GPF line; copied next to the maps so the directory is self-contained.
        shutil.copy2(parameter_file, work / "parameters.dat")
        header = ["parameter_file parameters.dat"]
    npts = [_npts(size[i], spacing) for i in range(3)]
    gpf = work / f"{stem}.gpf"
    gpf.write_text(
        "\n".join(
            [
                *header,
                f"npts {npts[0]} {npts[1]} {npts[2]}",
                f"gridfld {stem}.maps.fld",
                f"spacing {spacing}",
                f"receptor_types {' '.join(atom_types(receptor_pdbqt))}",
                f"ligand_types {' '.join(ligand_types)}",
                f"receptor {local_receptor.name}",
                f"gridcenter {center[0]:.3f} {center[1]:.3f} {center[2]:.3f}",
                "smooth 0.5",
                *[f"map {stem}.{t}.map" for t in ligand_types],
                f"elecmap {stem}.e.map",
                f"dsolvmap {stem}.d.map",
                "dielectric -0.1465",
                "",
            ]
        ),
        encoding="utf-8",
    )
    command = [AUTOGRID4, "-p", gpf.name, "-l", f"{stem}.glg"]
    proc = subprocess.run(command, cwd=str(work), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"autogrid4 failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}")
    return work / f"{stem}.maps.fld"


def map_prefix(
    *,
    cache: dict[tuple, str],
    maps_dir: Path,
    receptor_path: Path,
    ligand_path: Path,
    box_center: list[float],
    box_size: list[float],
    spacing: float,
    flex_receptor_path: Path | None = None,
    parameter_file: Path | None = None,
) -> str:
    """Maps for one (receptor, box, atom types, parameters), returning their path prefix
    ("<prefix>.maps.fld"). Cached so identical-typed ligands share maps. Flexible side chains
    move through the same maps, so their atom types get a map too.
    ponytail: autogrid runs once per distinct atom-type set; union-per-receptor if it's too slow.
    """
    moving = [ligand_path, *([flex_receptor_path] if flex_receptor_path is not None else [])]
    ligand_types = tuple(sorted({atom_type for path in moving for atom_type in atom_types(path)}))
    key = (
        str(receptor_path),
        tuple(round(float(v), 3) for v in box_center),
        tuple(round(float(v), 3) for v in box_size),
        round(float(spacing), 4),
        ligand_types,
        # Content, not path: editing the file in place must not reuse the old maps.
        hashlib.md5(parameter_file.read_bytes()).hexdigest() if parameter_file is not None else "",
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    if not (Path(AUTOGRID4).exists() or shutil.which(str(AUTOGRID4))):
        raise RuntimeError(
            f"The AutoDock4 force field needs autogrid4 but it was not found ({AUTOGRID4!r}). "
            "Set $AMDOCK_AUTOGRID4 or run `conda install -c conda-forge autogrid`."
        )
    work = Path(maps_dir) / f"{receptor_path.stem}_{hashlib.md5(repr(key).encode()).hexdigest()[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    autogrid(receptor_path, list(ligand_types), list(box_center), list(box_size), float(spacing), work, parameter_file)
    prefix = str(work / receptor_path.stem)
    cache[key] = prefix
    return prefix


__all__ = ["AUTOGRID4", "atom_types", "autogrid", "map_prefix"]
