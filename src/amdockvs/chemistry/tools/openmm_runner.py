"""PDBFixer repair and OpenMM minimization, run as a script by the OpenMM tool environment.

Standalone on purpose: that environment has OpenMM and PDBFixer but not AMDockVS, so this file
imports only the standard library and those two. Called as

    <env python> openmm_runner.py '{"op": "fix" | "minimize", "source": ..., "output": ..., ...}'
"""
from __future__ import annotations

import json
import sys


def fix(source: str, output: str, add_missing_residues: bool = True, add_missing_atoms: bool = True,
        replace_nonstandard: bool = True, remove_heterogens: bool = False, keep_water: bool = True) -> None:
    from openmm.app import PDBFile
    from pdbfixer import PDBFixer

    fixer = PDBFixer(filename=source)
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
    with open(output, "w", encoding="utf-8") as handle:
        PDBFile.writeFile(fixer.topology, fixer.positions, handle)


def minimize(source: str, output: str, forcefields: list[str], max_iterations: int,
             tolerance_kj_mol: float) -> None:
    from openmm import LangevinIntegrator, unit
    from openmm.app import ForceField, HBonds, Modeller, NoCutoff, PDBFile, Simulation

    pdb = PDBFile(source)
    forcefield = ForceField(*forcefields)
    # Hydrogens placed by reduce/pdb2pqr don't match the force field templates (terminal
    # residues above all), so rebuild them here — heavy atoms, and the pose, stay untouched.
    model = Modeller(pdb.topology, pdb.positions)
    model.delete([atom for atom in model.topology.atoms() if atom.element is not None and atom.element.symbol == "H"])
    model.addHydrogens(forcefield)
    system = forcefield.createSystem(model.topology, nonbondedMethod=NoCutoff, constraints=HBonds)
    integrator = LangevinIntegrator(300 * unit.kelvin, 1 / unit.picosecond, 0.002 * unit.picoseconds)
    simulation = Simulation(model.topology, system, integrator)
    simulation.context.setPositions(model.positions)
    simulation.minimizeEnergy(
        tolerance=float(tolerance_kj_mol) * unit.kilojoule_per_mole / unit.nanometer,
        maxIterations=int(max_iterations),
    )
    positions = simulation.context.getState(getPositions=True).getPositions()
    with open(output, "w", encoding="utf-8") as handle:
        PDBFile.writeFile(simulation.topology, positions, handle)


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    {"fix": fix, "minimize": minimize}[request.pop("op")](**request)
