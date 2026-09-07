"""Building the (ligand, receptor) payloads a docking chunk consumes.

Everything a worker needs travels in the pair: prepared paths, the box, the protocol
options and the import-time descriptors. The worker re-reads nothing from the database,
which is what lets a chunk run on another machine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from amdockvs.core.constants import DEFAULT_VINA_BACKEND, DEFAULT_VINA_COMMAND
from amdockvs.docking.engines.programs import chunk_resources
from amdockvs.docking.preparation.state import docking_input_path_from_row, grid_from_row


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
    engine: str = "vina",
    complex_id: int | None = None,
    run_kind: str = "screening",
    box_center: list[float] | tuple[float, float, float] | None = None,
    box_size: list[float] | tuple[float, float, float] | None = None,
    spacing: float | None = None,
    reference_ligand_path: str | Path | None = None,
    reference_receptor_path: str | Path | None = None,
) -> dict[str, object]:
    ligand_source_path = str(ligand_row.get("current_path") or ligand_row.get("stored_path") or "")
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "ligand_artifact_id": int(ligand_row.get("artifact_id") or 0) or None,
        "receptor_artifact_id": int(receptor_row.get("artifact_id") or 0) or None,
        "ligand_path": str(docking_input_path_from_row(ligand_row, engine=engine)),
        "ligand_source_path": ligand_source_path,
        "ligand_descriptors": _ligand_descriptors_from_row(ligand_row),
        "receptor_path": str(docking_input_path_from_row(receptor_row, engine=engine)),
        "reference_ligand_path": str(reference_ligand_path or ""),
        "reference_receptor_path": str(reference_receptor_path or ""),
        "exhaustiveness": int(exhaustiveness),
        "num_modes": int(num_modes),
        "box_center": None if box_center is None else [float(value) for value in box_center],
        "box_size": None if box_size is None else [float(value) for value in box_size],
        "spacing": None if spacing is None else float(spacing),
    }


def build_failed_docking_pair(
    *,
    ligand_row: Mapping[str, object],
    receptor_row: Mapping[str, object],
    reason: str,
    complex_id: int | None = None,
    run_kind: str = "screening",
    reference_ligand_path: str | Path | None = None,
    reference_receptor_path: str | Path | None = None,
) -> dict[str, object]:
    ligand_source_path = str(ligand_row.get("current_path") or ligand_row.get("stored_path") or "")
    return {
        "complex_id": None if complex_id is None else int(complex_id),
        "run_kind": str(run_kind or "screening"),
        "ligand_id": int(ligand_row.get("id") or 0),
        "receptor_id": int(receptor_row.get("id") or 0),
        "ligand_artifact_id": int(ligand_row.get("artifact_id") or 0) or None,
        "receptor_artifact_id": int(receptor_row.get("artifact_id") or 0) or None,
        "ligand_path": str(ligand_row.get("prepared_engine_path") or ligand_row.get("current_path") or ligand_row.get("stored_path") or ""),
        "ligand_source_path": ligand_source_path,
        "receptor_path": str(receptor_row.get("prepared_engine_path") or receptor_row.get("current_path") or receptor_row.get("stored_path") or ""),
        "reference_ligand_path": str(reference_ligand_path or ""),
        "reference_receptor_path": str(reference_receptor_path or ""),
        "invalid_reason": str(reason or "Invalid docking pair."),
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
    vina_backend: str = DEFAULT_VINA_BACKEND,
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
) -> Iterator[dict[str, object]]:
    # Prepared inputs/grids are stored under the preparation engine (e.g. AutoDock4
    # reuses Vina pdbqt prep); the chunk's `engine` tag selects the docking runner.
    prep_engine = str(preparation_engine or engine)
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
        for ligand in ligands_for(int(receptor_row.get("id") or 0)):
            ligand_row = dict(ligand)
            pair_grid = None
            effective_center = box_center
            effective_size = box_size
            effective_spacing = spacing
            if requires_binding_site and (effective_center is None or effective_size is None):
                pair_grid = grid_from_row(receptor_row, engine=prep_engine)
                if pair_grid is not None:
                    resolved_center = tuple(float(value) for value in (pair_grid.get("center") or ()))
                    resolved_size = tuple(float(value) for value in (pair_grid.get("size") or ()))
                    if len(resolved_center) == 3 and len(resolved_size) == 3:
                        effective_center = resolved_center
                        effective_size = resolved_size
                        effective_spacing = float(pair_grid.get("spacing") or spacing)
            if requires_binding_site and (effective_center is None or effective_size is None):
                batch.append(
                    build_failed_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        reason=(
                            f"Receptor {int(receptor_row.get('id') or 0)} requires explicit "
                            "box_center/box_size or a stored grid."
                        ),
                    )
                )
            else:
                try:
                    batch.append(
                        build_docking_pair(
                            ligand_row=ligand_row,
                            receptor_row=receptor_row,
                            exhaustiveness=exhaustiveness,
                            num_modes=num_modes,
                            engine=prep_engine,
                            box_center=effective_center,
                            box_size=effective_size,
                            spacing=effective_spacing,
                        )
                    )
                except Exception as exc:
                    batch.append(
                        build_failed_docking_pair(
                            ligand_row=ligand_row,
                            receptor_row=receptor_row,
                            reason=str(exc),
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
                    "vina_backend": str(vina_backend or "python"),
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
            "vina_backend": str(vina_backend or "python"),
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
