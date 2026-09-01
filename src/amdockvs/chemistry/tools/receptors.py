from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required executable '{name}' was not found on PATH.")
    return executable


def protonate_receptor_reduce_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    executable = _require_executable("reduce")
    output.parent.mkdir(parents=True, exist_ok=True)
    # -BUILD on a structure that already carries hydrogens duplicates them, so trim first.
    # reduce exits 255 even on a good -Trim, so the output itself is the success check.
    trimmed = subprocess.run(
        [executable, "-Trim", "-Quiet", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if not trimmed.stdout.strip():
        raise RuntimeError(f"reduce -Trim produced no output: {trimmed.stderr.strip()[:400]}")
    built = subprocess.run(
        [executable, "-BUILD", "-Quiet", "-"],
        input=trimmed.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    output.write_text(built.stdout, encoding="utf-8")
    return output


def protonate_receptor_pdb2pqr_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
    forcefield: str = "AMBER",
    ph: float = 7.0,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    executable = _require_executable("pdb2pqr")
    output.parent.mkdir(parents=True, exist_ok=True)
    # `output` is the PDB, not the PQR: PQR has no element column and puts charge/radius where
    # occupancy/B-factor belong, so every consumer that reads elements (RDKit -> Meeko receptor
    # prep, our complex builder) sees an empty element and dies. pdb2pqr writes both in one run,
    # and the PQR stays beside it for whoever wants its charges.
    pqr = output.with_suffix(".pqr")
    subprocess.run(
        [
            executable,
            f"--ff={forcefield}",
            f"--with-ph={float(ph)}",
            "--keep-chain",  # without it the PQR drops chain ids and residue refs stop matching
            f"--pdb-output={output}",
            str(source),
            str(pqr),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return output


def fix_receptor_pdb_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
    add_missing_residues: bool = True,
    add_missing_atoms: bool = True,
    replace_nonstandard: bool = True,
    remove_heterogens: bool = False,
    keep_water: bool = True,
) -> Path:
    try:
        from pdbfixer import PDBFixer
    except ImportError as exc:
        raise RuntimeError("Python package 'pdbfixer' is not available in the current environment.") from exc

    try:
        from openmm.app import PDBFile
    except ImportError:
        try:
            from simtk.openmm.app import PDBFile  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Python package 'openmm' is required by pdbfixer but is not available.") from exc

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    fixer = PDBFixer(filename=str(source))

    if replace_nonstandard:
        fixer.findNonstandardResidues()
        fixer.replaceNonstandardResidues()
    if remove_heterogens:
        fixer.removeHeterogens(keepWater=bool(keep_water))
    if add_missing_residues:
        fixer.findMissingResidues()
    else:
        fixer.missingResidues = {}
    fixer.findMissingAtoms()
    if add_missing_atoms:
        fixer.addMissingAtoms()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(fixer.topology, fixer.positions, handle)
    return output


def minimize_receptor_openmm_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
    forcefields: Iterable[str],
    max_iterations: int,
    tolerance_kj_mol: float,
) -> Path:
    try:
        from openmm import LangevinIntegrator, unit
        from openmm.app import ForceField, HBonds, Modeller, NoCutoff, PDBFile, Simulation
    except ImportError as exc:
        raise RuntimeError("Python package 'openmm' is not available in the current environment.") from exc

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    pdb = PDBFile(str(source))
    forcefield = ForceField(*tuple(forcefields))
    # Hydrogens placed by reduce/pdb2pqr don't match the force field templates (terminal
    # residues above all), so rebuild them here — heavy atoms, and the pose, stay untouched.
    model = Modeller(pdb.topology, pdb.positions)
    model.delete([atom for atom in model.topology.atoms() if atom.element is not None and atom.element.symbol == "H"])
    model.addHydrogens(forcefield)
    system = forcefield.createSystem(
        model.topology,
        nonbondedMethod=NoCutoff,
        constraints=HBonds,
    )
    integrator = LangevinIntegrator(
        300 * unit.kelvin,
        1 / unit.picosecond,
        0.002 * unit.picoseconds,
    )
    simulation = Simulation(model.topology, system, integrator)
    simulation.context.setPositions(model.positions)
    simulation.minimizeEnergy(
        tolerance=float(tolerance_kj_mol) * unit.kilojoule_per_mole / unit.nanometer,
        maxIterations=int(max_iterations),
    )
    positions = simulation.context.getState(getPositions=True).getPositions()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(simulation.topology, positions, handle)
    return output


__all__ = [
    "fix_receptor_pdb_file",
    "minimize_receptor_openmm_file",
    "protonate_receptor_pdb2pqr_file",
    "protonate_receptor_reduce_file",
]
