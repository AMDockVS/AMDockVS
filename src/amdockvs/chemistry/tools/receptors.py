from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Iterable


def _require_executable(name: str, command: str | None = None) -> str:
    from ms_env import READY, locate
    from amdockvs.integrations.envs import protonation_tool_status, tool_spec

    spec = tool_spec(name)
    # A submitted absolute path may refer to a configured environment. Recheck its
    # version on the execution host; never interpret a bare name through PATH.
    if command and Path(command).is_absolute():
        recipe = spec.recipe("", prefix=str(Path(command).parent.parent))
        if command in recipe.candidates:
            state, executable = locate(replace(recipe, candidates=(command,), commands=()))
            if state == READY:
                return executable
    # Remote workers resolve their own managed installation when the submitted
    # path is absent or incompatible on that host.
    status = protonation_tool_status(None, name)
    if status.installed:
        return str(status.command)
    raise RuntimeError(
        f"Required {name} {spec.version} is not available on this machine. "
        "Install it from Settings → External tools → Protein preparation."
    )


def protonate_receptor_reduce_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
    command: str | None = None,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    executable = _require_executable("reduce", command)
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
    command: str | None = None,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    executable = _require_executable("pdb2pqr", command)
    output.parent.mkdir(parents=True, exist_ok=True)
    # `output` is the PDB, not the PQR: PQR has no element column and puts charge/radius where
    # occupancy/B-factor belong, so every consumer that reads elements (RDKit -> Meeko receptor
    # prep, our complex builder) sees an empty element and dies. pdb2pqr writes both in one run,
    # and the PQR lands beside it, in the work folder `run_receptor_tool` removes on return.
    pqr = output.with_suffix(".pqr")
    subprocess.run(
        [
            executable,
            f"--ff={forcefield}",
            "--titration-state-method=propka",
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


def _run_openmm(op: str, source_path: str | Path, output_path: str | Path, command: str | None, **params) -> Path:
    """Run PDBFixer/OpenMM in their own environment: neither is installed in the application's."""
    import json

    python = _require_executable("openmm", command)
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    request = {"op": op, "source": str(Path(source_path).expanduser().resolve()), "output": str(output), **params}
    result = subprocess.run([python, str(Path(__file__).with_name("openmm_runner.py")), json.dumps(request)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"OpenMM {op} failed: {result.stderr.strip()[-2000:]}")
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
    command: str | None = None,
) -> Path:
    return _run_openmm(
        "fix", source_path, output_path, command,
        add_missing_residues=bool(add_missing_residues), add_missing_atoms=bool(add_missing_atoms),
        replace_nonstandard=bool(replace_nonstandard), remove_heterogens=bool(remove_heterogens),
        keep_water=bool(keep_water),
    )


def minimize_receptor_openmm_file(
    *,
    source_path: str | Path,
    output_path: str | Path,
    forcefields: Iterable[str],
    max_iterations: int,
    tolerance_kj_mol: float,
    command: str | None = None,
) -> Path:
    return _run_openmm(
        "minimize", source_path, output_path, command,
        forcefields=list(forcefields), max_iterations=int(max_iterations), tolerance_kj_mol=float(tolerance_kj_mol),
    )


__all__ = [
    "fix_receptor_pdb_file",
    "minimize_receptor_openmm_file",
    "protonate_receptor_pdb2pqr_file",
    "protonate_receptor_reduce_file",
]
