"""Compose the ligand chemistry cores into one batch -> batch pass.

Pure: no `Path`, no database, no `output_dir`. A list of molecules goes in, a list of the
same length comes out, and the same function serves both modes — `transform_ligand_rows`
runs it over rows of the project db, `transform_ligand_shard` over a shard file.

Two contracts make that possible:

**Batch, not molecule.** Steps take the whole batch, because protonation is genuinely a
batch operation: Dimorphite and OpenBabel run once over a set (`protonation.py:152`). A
per-molecule pipeline would have to break protonation apart to fit.

**Position is identity.** The output list is the same length and order as the input, so the
caller can zip it back against whatever it loaded the molecules from. A molecule that fails
does not vanish — its slot holds the exception, and later steps pass it through untouched.
Raising instead would throw away 999 good molecules for one bad one.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Iterable, Sequence

from amdockvs.chemistry.conformers import generate_conformer_ensemble
from amdockvs.chemistry.protonation import protonate_molecule_batch
from amdockvs.chemistry.tools import (
    generate_ligand_3d,
    minimize_ligand_molecule,
    standardize_ligand_molecule,
)

# A slot is a molecule or the exception that replaced it.
Slot = Any
Step = Callable[..., list[Slot]]


def kwargs_for(func: Callable[..., Any], params) -> dict:
    """Keep only the keys `func` declares.

    Callers hand every step the same bag of parameters (`ph`, `run_id`, `num_conformers`, ...)
    and each core takes the ones it understands. An undeclared key was already ignored before,
    when the service picked keys by hand — this just stops doing it in five places.
    """
    signature = inspect.signature(func)
    if any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
        return dict(params)
    return {key: value for key, value in dict(params).items() if key in signature.parameters}


def _lift(core: Callable[..., Any]) -> Step:
    """Turn a molecule -> molecule core into a batch step that keeps position and failures."""

    def step(mols: Sequence[Slot], **params) -> list[Slot]:
        kwargs = kwargs_for(core, params)
        out: list[Slot] = []
        for mol in mols:
            if isinstance(mol, Exception) or mol is None:
                out.append(mol)
                continue
            try:
                out.append(core(mol, **kwargs))
            except Exception as exc:  # noqa: BLE001 — the slot carries it to the caller
                out.append(exc)
        return out

    return step


def _protonate(mols: Sequence[Slot], **params) -> list[Slot]:
    """The one genuinely batched step: one call for the whole set, then scatter back.

    `protonate_molecule_batch` keys by a positive int id and may drop entries, so positions
    are used as ids (offset by one, since it discards id 0) and a missing result becomes an
    exception in its own slot rather than a shorter list.
    """
    method = str(params.get("method") or "dimorphite")
    entries = [
        (index + 1, mol)
        for index, mol in enumerate(mols)
        if not isinstance(mol, Exception) and mol is not None
    ]
    if not entries:
        return list(mols)
    try:
        results = protonate_molecule_batch(entries, method=method, params=params)
    except Exception as exc:  # noqa: BLE001 — one failed backend must not lose the batch
        return [mol if isinstance(mol, Exception) else exc for mol in mols]
    out: list[Slot] = []
    for index, mol in enumerate(mols):
        if isinstance(mol, Exception) or mol is None:
            out.append(mol)
            continue
        protonated = results.get(index + 1)
        out.append(
            protonated
            if protonated is not None
            else ValueError("The selected protonation method returned no structure.")
        )
    return out


def _conformers(mol, **params):
    ensemble, _conformer_ids = generate_conformer_ensemble(
        mol, **kwargs_for(generate_conformer_ensemble, params)
    )
    return ensemble


LIGAND_STEPS: dict[str, Step] = {
    "standardize": _lift(standardize_ligand_molecule),
    "protonate": _protonate,
    "generate_3d": _lift(generate_ligand_3d),
    "conformers": _lift(_conformers),
    "minimize": _lift(minimize_ligand_molecule),
}


def run_pipeline(mols: Iterable[Slot], steps: Sequence[tuple[str, dict]]) -> list[Slot]:
    """Run `steps` in order over the whole batch. Returns one slot per input molecule."""
    batch = list(mols)
    for name, params in steps:
        step = LIGAND_STEPS.get(name)
        if step is None:
            raise ValueError(f"Unsupported ligand chemistry operation: {name}")
        batch = step(batch, **dict(params or {}))
    return batch


def normalize_steps(operations) -> list[tuple[str, dict]]:
    """Accept a bare operation name, or (name, params) pairs, as a step list.

    Single-operation callers (`protonate_ligands()` and friends) stay one-liners.
    """
    if isinstance(operations, str):
        return [(operations.strip().lower(), {})]
    steps: list[tuple[str, dict]] = []
    for item in operations or ():
        if isinstance(item, str):
            steps.append((item.strip().lower(), {}))
            continue
        name, params = item
        steps.append((str(name).strip().lower(), dict(params or {})))
    return steps


__all__ = ["LIGAND_STEPS", "kwargs_for", "normalize_steps", "run_pipeline"]
