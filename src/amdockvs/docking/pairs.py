"""Building the (ligand, receptor) payloads a docking chunk consumes.

Everything a worker needs travels in the pair: the ligand's prepared bytes, the receptor's
files (written once per job, `input_path`/`flex_path` of its row), the box, the protocol
options and the import-time descriptors. The worker re-reads nothing from the database,
which is what lets a chunk run on another machine.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from amdockvs.core.constants import DEFAULT_VINA_COMMAND
from amdockvs.docking.engines.programs import chunk_resources, get_docking_program
from amdockvs.docking.methods import excludes
from amdockvs.docking.preparation.state import docking_input_payload_id, grid_from_row


def docking_input_key(
    receptor_row: Mapping[str, object],
    center: Iterable[float] | None,
    size: Iterable[float] | None,
    spacing: float | None,
) -> str:
    """What a pair was docked against, as a short hash: the receptor's docked input and flexible
    side chains (the payloads a re-preparation replaces) and the box. Stored on each result
    (`input_key`), it is what lets skip_existing reuse a result only for the same inputs."""
    try:
        receptor = int(receptor_row.get("input_payload_id") or docking_input_payload_id(receptor_row))
    except ValueError:
        receptor = 0

    def numbers(values):
        return None if values is None or not len(tuple(values)) else [round(float(v), 4) for v in values]

    identity = [receptor, int(receptor_row.get("flex_payload_id") or 0), numbers(center), numbers(size),
                None if spacing is None else round(float(spacing), 4)]
    return hashlib.sha1(json.dumps(identity).encode()).hexdigest()[:16]


def legacy_input_key(receptor_payload_id: object) -> str:
    """The key a result from before `input_key` was given (`_add_columns`): only its receptor
    payload is known, not its box."""
    return f"legacy:{int(receptor_payload_id or 0)}"


def matching_input_keys(receptor_row: Mapping[str, object], key: str) -> list[str]:
    """The stored keys that count as this receptor's current inputs: its key, and the legacy one
    of its current payload."""
    try:
        payload = receptor_row.get("input_payload_id") or docking_input_payload_id(receptor_row)
    except ValueError:
        return [key]
    return [key, legacy_input_key(payload)]


def receptor_box(
    receptor_row: Mapping[str, object],
    *,
    box_center: Iterable[float] | None,
    box_size: Iterable[float] | None,
    spacing: float,
    preparation_engine: str,
    requires_binding_site: bool = True,
) -> tuple[tuple[float, ...] | None, tuple[float, ...] | None, float]:
    """The box a receptor's screening pairs dock in: the explicit one, else its stored grid
    (only when the program needs a site). `(None, None, spacing)` without one."""
    center = None if box_center is None else tuple(float(v) for v in box_center)
    size = None if box_size is None else tuple(float(v) for v in box_size)
    if requires_binding_site and (center is None or size is None):
        grid = grid_from_row(receptor_row, engine=preparation_engine)
        if grid is not None:
            grid_center = tuple(float(v) for v in (grid.get("center") or ()))
            grid_size = tuple(float(v) for v in (grid.get("size") or ()))
            if len(grid_center) == 3 and len(grid_size) == 3:
                return grid_center, grid_size, float(grid.get("spacing") or spacing)
    return center, size, float(spacing)


def _ligand_descriptors_from_row(ligand_row: Mapping[str, object]) -> dict[str, float]:
    """The descriptors computed at import, shipped with the pair so the worker never re-parses
    the ligand file (and LLE/BEI/SEI stop coming out empty)."""
    fields = {"heavy_atoms": "heavy_atom_count", "molecular_weight": "mw", "clogp": "logp",
              "tpsa": "tpsa", "hbd": "hbd", "hba": "hba"}
    values = {key: ligand_row.get(column) for key, column in fields.items()}
    return {key: float(value) for key, value in values.items() if value is not None}


def build_docking_pair(
    *,
    ligand_row: Mapping[str, object],
    receptor_row: Mapping[str, object],
    exhaustiveness: int = 8,
    num_modes: int = 9,
    complex_id: int | None = None,
    run_kind: str = "screening",
    box_center: list[float] | tuple[float, float, float] | None = None,
    box_size: list[float] | tuple[float, float, float] | None = None,
    spacing: float | None = None,
    reference_ligand_path: object = "",
    reference_receptor_path: object = "",
    input_key: str = "",
) -> dict[str, object]:
    if ligand_row.get("ligand") is None:
        raise ValueError(f"Ligand {ligand_row.get('id')} has no structure to dock.")
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "ligand": ligand_row["ligand"],
        "ligand_descriptors": _ligand_descriptors_from_row(ligand_row),
        "receptor_path": receptor_row["input_path"],
        "receptor_payload_id": receptor_row.get("input_payload_id"),
        "flex_receptor_path": receptor_row.get("flex_path") or "",
        "reference_ligand_path": reference_ligand_path or "",
        "reference_receptor_path": reference_receptor_path or "",
        "exhaustiveness": int(exhaustiveness),
        "num_modes": int(num_modes),
        "box_center": None if box_center is None else [float(value) for value in box_center],
        "box_size": None if box_size is None else [float(value) for value in box_size],
        "spacing": None if spacing is None else float(spacing),
        "input_key": str(input_key or ""),
    }


def build_failed_docking_pair(
    *,
    ligand_row: Mapping[str, object],
    receptor_row: Mapping[str, object],
    reason: str,
    complex_id: int | None = None,
    run_kind: str = "screening",
    input_key: str = "",
) -> dict[str, object]:
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "invalid_reason": str(reason or "Invalid docking pair."),
        "input_key": str(input_key or ""),
    }


def iter_docking_batches_from_rows(
    *,
    ligands: Iterable[Mapping[str, object]] | Callable[[int], Iterable[Mapping[str, object]]],
    receptors: Iterable[Mapping[str, object]],
    output_dir: str | Path,
    batch_size: int,
    exhaustiveness: int = 8,
    num_modes: int = 9,
    box_center: list[float] | tuple[float, float, float] | None = None,
    box_size: list[float] | tuple[float, float, float] | None = None,
    scoring_function: str = "vina",
    vina_command: str = DEFAULT_VINA_COMMAND,
    vina_cpu: int = 1,
    seed: int = 0,
    spacing: float = 0.375,
    energy_range: float = 3.0,
    min_rmsd: float = 1.0,
    run_id: str = "",
    protocol_metadata: Mapping[str, object] | None = None,
    engine_config: Mapping[str, object] | None = None,
    engine: str = "vina",
    preparation_engine: str | None = None,
    requires_binding_site: bool = True,
    keep: Callable[[Mapping[str, object]], bool] | None = None,
) -> Iterator[dict[str, object]]:
    """Batches of receptor-ligand pairs. `keep` drops pairs before they join a batch: what a
    query cannot answer (`InputSource.filter`), so a batch stays full."""
    # Prepared inputs/grids are stored under the preparation engine (e.g. AutoDock-GPU
    # reuses Vina pdbqt prep); the chunk's `engine` tag selects the docking runner.
    prep_engine = str(preparation_engine or get_docking_program(engine).preparation_engine)
    method = dict(engine_config or {}).get("method")
    protocol_payload = dict(protocol_metadata or {})
    normalized_batch_size = max(1, int(batch_size))
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    batch_index = 0

    receptor_rows = [dict(row) for row in receptors]
    if not receptor_rows:
        return

    # Receptor-major: each receptor gets its own ligand stream, already filtered by the
    # database (the "already docked" guard is a subquery, not a set held in memory).
    if callable(ligands):
        ligands_for = ligands
    elif not isinstance(ligands, Iterator):
        ligands_for = lambda _receptor_id: ligands  # noqa: E731 — re-iterable by construction
    else:
        raise TypeError(
            "ligands must be a callable(receptor_id) -> iterable, or a re-iterable source: "
            "a plain iterator would be exhausted after the first receptor."
        )

    batch: list[dict[str, object]] = []
    for receptor_row in receptor_rows:
        effective_center, effective_size, effective_spacing = receptor_box(
            receptor_row, box_center=box_center, box_size=box_size, spacing=spacing,
            preparation_engine=prep_engine, requires_binding_site=requires_binding_site,
        )
        input_key = docking_input_key(receptor_row, effective_center, effective_size, effective_spacing)
        for ligand in ligands_for(int(receptor_row.get("id") or 0)):
            ligand_row = dict(ligand)
            if requires_binding_site and (effective_center is None or effective_size is None):
                batch.append(
                    build_failed_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        reason=(
                            f"Receptor {int(receptor_row.get('id') or 0)} requires explicit "
                            "box_center/box_size or a stored grid."
                        ),
                        input_key=input_key,
                    )
                )
            else:
                try:
                    pair = build_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        exhaustiveness=exhaustiveness,
                        num_modes=num_modes,
                        box_center=effective_center,
                        box_size=effective_size,
                        spacing=effective_spacing,
                        input_key=input_key,
                    )
                    if effective_center is not None and excludes(
                        method, pair["receptor_path"], effective_center, effective_size
                    ):
                        break  # the receptor does not fit the method: none of its pairs run
                    if keep is None or keep(pair):
                        batch.append(pair)
                except Exception as exc:
                    batch.append(
                        build_failed_docking_pair(
                            ligand_row=ligand_row,
                            receptor_row=receptor_row,
                            reason=str(exc),
                            input_key=input_key,
                        )
                    )
            if len(batch) >= normalized_batch_size:
                batch_index += 1
                yield {
                    "pairs": list(batch),
                    "engine": str(engine),
                    "output_dir": str(resolved_output_dir),
                    "box_center": [],
                    "box_size": [],
                    "scoring_function": str(scoring_function),
                    "vina_command": str(vina_command or "vina"),
                    "vina_cpu": int(vina_cpu),
                    "seed": int(seed),
                    "spacing": float(spacing),
                    "energy_range": float(energy_range),
                    "min_rmsd": float(min_rmsd),
                    "run_id": str(run_id or ""),
                    "protocol_metadata": protocol_payload,
                    "engine_config": dict(engine_config or {}),
                    **chunk_resources(engine, {**dict(engine_config or {}), "scoring_function": scoring_function}),
                    "report_name": f"batch_{batch_index:06d}.json",
                }
                batch = []
    if batch:
        batch_index += 1
        yield {
            "pairs": list(batch),
            "engine": str(engine),
            "output_dir": str(resolved_output_dir),
            "box_center": [],
            "box_size": [],
            "scoring_function": str(scoring_function),
            "vina_command": str(vina_command or "vina"),
            "vina_cpu": int(vina_cpu),
            "seed": int(seed),
            "spacing": float(spacing),
            "energy_range": float(energy_range),
            "min_rmsd": float(min_rmsd),
            "run_id": str(run_id or ""),
            "protocol_metadata": protocol_payload,
            "engine_config": dict(engine_config or {}),
            **chunk_resources(engine, {**dict(engine_config or {}), "scoring_function": scoring_function}),
            "report_name": f"batch_{batch_index:06d}.json",
        }


__all__ = [
    "build_docking_pair",
    "build_failed_docking_pair",
    "iter_docking_batches_from_rows",
]
