from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ms_flow.payloads import scratch_file


def _molecule_id(molecule) -> int:
    for key in ("_AMDockID", "_Name"):
        if molecule.HasProp(key):
            try:
                return int(molecule.GetProp(key))
            except ValueError:
                pass
    return 0


def _read_sdf_by_id(path: Path) -> dict[int, Any]:
    from rdkit import Chem

    results: dict[int, Any] = {}
    for molecule in Chem.SDMolSupplier(str(path), sanitize=True, removeHs=False):
        if molecule is None:
            continue
        entity_id = _molecule_id(molecule)
        if entity_id > 0:
            results[entity_id] = molecule
    return results


def _sdf_bytes(entries: Sequence[tuple[int, Any]]) -> bytes:
    from rdkit import Chem

    buffer = io.StringIO()
    writer = Chem.SDWriter(buffer)
    try:
        for entity_id, source in entries:
            molecule = Chem.Mol(source)
            molecule.SetProp("_Name", str(entity_id))
            molecule.SetProp("_AMDockID", str(entity_id))
            writer.write(molecule)
    finally:
        writer.close()
    return buffer.getvalue().encode()


def _run(command: list[str], *, timeout: int) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Protonation command failed: {detail}")


def _dimorphite(entries: Sequence[tuple[int, Any]], *, ph: float) -> dict[int, Any]:
    from dimorphite_dl import protonate_smiles
    from rdkit import Chem

    smiles = [Chem.MolToSmiles(Chem.RemoveHs(molecule)) for _entity_id, molecule in entries]
    protonated = protonate_smiles(
        smiles,
        ph_min=float(ph),
        ph_max=float(ph),
        precision=0.0,
        max_variants=1,
    )
    results: dict[int, Any] = {}
    for (entity_id, _source), value in zip(entries, protonated):
        molecule = Chem.MolFromSmiles(str(value))
        if molecule is not None:
            results[entity_id] = molecule
    return results


def _openbabel(
    entries: Sequence[tuple[int, Any]],
    *,
    command: Path,
    ph: float,
) -> dict[int, Any]:
    with scratch_file(_sdf_bytes(entries), ".sdf") as input_path, scratch_file(b"", ".sdf") as output_path:
        _run(
            [str(command), "-isdf", str(input_path), "-osdf", "-O", str(output_path), "-p", str(float(ph))],
            timeout=max(600, len(entries) * 20),
        )
        return _read_sdf_by_id(output_path)


def _pkasso(
    entries: Sequence[tuple[int, Any]],
    *,
    python: Path,
    ph: float,
    model: str,
    threads: int,
    gpu: bool,
) -> dict[int, Any]:
    from rdkit import Chem

    request = json.dumps(
        [{"id": entity_id, "smiles": Chem.MolToSmiles(Chem.RemoveHs(molecule))} for entity_id, molecule in entries]
    ).encode()
    with scratch_file(request, ".json") as input_path, scratch_file(b"", ".sdf") as output_path:
        runner = Path(__file__).with_name("pkasso_runner.py").resolve()
        command = [
            str(python),
            str(runner),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--ph",
            str(float(ph)),
            "--model",
            str(model),
            "--threads",
            str(max(1, int(threads))),
        ]
        if gpu:
            command.append("--gpu")
        _run(command, timeout=max(1800, len(entries) * 300))
        return _read_sdf_by_id(output_path)


def protonate_molecule_batch(
    molecules: Sequence[tuple[int, Any]],
    *,
    method: str,
    params: Mapping[str, Any],
) -> dict[int, Any]:
    entries = [(int(entity_id), molecule) for entity_id, molecule in molecules if int(entity_id) > 0]
    if not entries:
        return {}
    normalized = str(method or "dimorphite").strip().lower()
    ph = float(params.get("ph", 7.4))
    if normalized == "dimorphite":
        results = _dimorphite(entries, ph=ph)
    elif normalized == "explicit_hs":
        # No pH model: the input charge states stay as they are; the AddHs below fills every H.
        results = dict(entries)
    elif normalized == "openbabel":
        command = Path(str(params.get("tool_command") or "")).expanduser().resolve()
        if not command.is_file():
            raise RuntimeError("OpenBabel is not installed. Install its runtime from Build first.")
        results = _openbabel(entries, command=command, ph=ph)
    elif normalized == "pkasso":
        python = Path(str(params.get("tool_command") or "")).expanduser().resolve()
        if not python.is_file():
            raise RuntimeError("pKasso is not installed. Install its runtime from Build first.")
        results = _pkasso(
            entries,
            python=python,
            ph=ph,
            model=str(params.get("model") or "molgpka"),
            threads=int(params.get("threads", 1)),
            gpu=bool(params.get("gpu", False)),
        )
    else:
        raise ValueError(f"Unsupported small-molecule protonation method: {method}")

    from rdkit import Chem

    return {entity_id: Chem.AddHs(Chem.Mol(molecule), addCoords=True) for entity_id, molecule in results.items()}


__all__ = ["protonate_molecule_batch"]
