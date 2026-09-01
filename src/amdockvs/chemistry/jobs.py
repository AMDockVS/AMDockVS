from __future__ import annotations

from datetime import datetime
from itertools import batched
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.sinks import graph_sink
from ms_flow.tasking import job, task

from amdockvs.constants import AMDOCKVS_LOCAL_EXECUTORS
from amdockvs.api_common import (
    project_root_from_output_dir,
    restore_worker_paths,
    worker_output_dir,
    worker_path_fields,
)
from amdockvs.chemistry.repository import (
    iter_ligand_rows,
    iter_receptor_rows,
    max_model_index_by_molecule_ids,
    project_db_path,
    resolve_ligand_storage_dir,
    resolve_receptor_storage_dir,
)
from amdockvs.chemistry.service import transform_ligand_rows, transform_receptor_rows
from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.molecule_paths import set_default_project_root


# Chemistry operations update existing molecules (upsert by id) and may add new
# conformer rows (insert), so the job emits a two-node graph payload.
CHEMISTRY_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": [
                "id",
                "extra_data",
                "has_3d",
                "has_hs",
                "is_minimized",
                "conformer_count",
                "current_path",
                "current_model_index",
                "updated_at",
            ],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
        {"name": "molecule_models", "model": MoleculeModel, "validate_model": False},
    ),
)


def _chemistry_graph_payload(updates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map chemistry updates into the molecules-upsert / molecule_models-insert graph payload."""
    now = datetime.now()
    molecules: list[dict[str, Any]] = []
    molecule_models: list[dict[str, Any]] = []
    for update in updates:
        entity_id = int(update.get("entity_id") or 0)
        if entity_id <= 0:
            continue
        mol_row: dict[str, Any] = {
            "id": entity_id,
            "extra_data": dict(update.get("extra_data") or {}),
            "updated_at": now,
        }
        state = dict(update.get("state") or {})
        for key in ("has_3d", "has_hs", "is_minimized", "conformer_count"):
            if key in state:
                mol_row[key] = state[key]
        if "current_path" in update:
            mol_row["current_path"] = str(update.get("current_path") or "")
        if "current_model_index" in update:
            current_model_index = update.get("current_model_index")
            mol_row["current_model_index"] = None if current_model_index is None else int(current_model_index)
        molecules.append(mol_row)
        for model_row in list(update.get("model_rows") or []):
            molecule_models.append(dict(model_row))
    return {"molecules": molecules, "molecule_models": molecule_models}


class LigandChemistryJobParams(BaseModel):
    operation: str
    batch_size: int = Field(default=128, ge=1)
    ligand_set_id: int | None = Field(default=None, ge=1)
    ligand_filters: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class ReceptorChemistryJobParams(BaseModel):
    operation: str
    batch_size: int = Field(default=32, ge=1)
    receptor_set_id: int | None = Field(default=None, ge=1)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


def _chemistry_batches(
    *,
    project_db,
    db_path: Path,
    output_dir: Path,
    params,
    rows: Iterator[dict[str, Any]],
    gpu: bool = False,
) -> Iterator[dict[str, Any]]:
    """One batch of rows = one chunk. `batched` comes from itertools: no hand-rolled accumulator
    and no duplicated final-flush block, which is where the two copies of this came from."""
    for batch in batched(rows, max(1, int(params.batch_size))):
        batch_rows = list(batch)
        batch_ids = [int(row.get("id") or 0) for row in batch_rows if int(row.get("id") or 0) > 0]
        max_index_map = max_model_index_by_molecule_ids(project_db, batch_ids)
        yield {
            "operation": params.operation,
            "output_dir": worker_output_dir(output_dir),
            "params": dict(params.params or {}),
            "rows": [worker_path_fields(row, "stored_path", "current_path") for row in batch_rows],
            "next_model_index_by_entity": {
                entity_id: int(max_index_map.get(entity_id, -1)) + 1 for entity_id in batch_ids
            },
            **({"_gpu_required": 1} if gpu else {}),
        }


def _iter_ligand_chemistry_batches(
    *,
    project_db,
    db_path: Path,
    output_dir: Path,
    params: LigandChemistryJobParams,
) -> Iterator[dict[str, Any]]:
    return _chemistry_batches(
        project_db=project_db,
        db_path=db_path,
        output_dir=output_dir,
        params=params,
        rows=iter_ligand_rows(
            project_db,
            ligand_set_id=params.ligand_set_id,
            filters=params.ligand_filters,
            batch_size=params.batch_size,
        ),
        gpu=params.operation == "protonate" and bool(params.params.get("gpu")),
    )


def _iter_receptor_chemistry_batches(
    *,
    project_db,
    db_path: Path,
    output_dir: Path,
    params: ReceptorChemistryJobParams,
) -> Iterator[dict[str, Any]]:
    return _chemistry_batches(
        project_db=project_db,
        db_path=db_path,
        output_dir=output_dir,
        params=params,
        rows=iter_receptor_rows(
            project_db,
            receptor_set_id=params.receptor_set_id,
            filters=params.receptor_filters,
            batch_size=params.batch_size,
        ),
    )


@task(
    name="amdock_ligand_chemistry_batch",
    description="Apply a reusable ligand chemistry operation and persist metadata updates.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def ligand_chemistry_task(payload: dict, progress_cb=None):
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    set_default_project_root(project_root_from_output_dir(output_dir))
    rows = list(payload.get("rows") or [])
    result = transform_ligand_rows(
        operation=str(payload.get("operation") or ""),
        output_dir=output_dir,
        rows=rows,
        params=dict(payload.get("params") or {}),
        next_model_index_by_entity=dict(payload.get("next_model_index_by_entity") or {}),
        progress_cb=progress_cb,
    )
    updates = list(result.get("updates") or [])
    failure_count = int(result.get("failure_count") or 0)
    processed_count = int(result.get("processed_count") or len(updates) + failure_count)
    if processed_count > 0 and not updates and failure_count > 0:
        sample = list(result.get("failure_samples") or [])
        raise RuntimeError(
            "Ligand chemistry batch failed for every molecule. "
            f"processed={processed_count} failed={failure_count} samples={sample[:3]}"
        )
    if failure_count > 0:
        print(
            "Ligand chemistry batch completed with skipped molecules: "
            f"updated={len(updates)} failed={failure_count} "
            f"samples={result.get('failure_samples') or []}"
        )
    return _chemistry_graph_payload(restore_worker_paths(updates, rows))


@task(
    name="amdock_receptor_chemistry_batch",
    description="Apply a reusable receptor chemistry operation and persist metadata updates.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def receptor_chemistry_task(payload: dict, progress_cb=None):
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    set_default_project_root(project_root_from_output_dir(output_dir))
    rows = list(payload.get("rows") or [])
    updates = transform_receptor_rows(
        operation=str(payload.get("operation") or ""),
        output_dir=output_dir,
        rows=rows,
        params=dict(payload.get("params") or {}),
        next_model_index_by_entity=dict(payload.get("next_model_index_by_entity") or {}),
        progress_cb=progress_cb,
    )
    if list(payload.get("rows") or []) and not updates:
        raise RuntimeError("Receptor chemistry batch produced no updates.")
    return _chemistry_graph_payload(restore_worker_paths(list(updates or []), rows))


@job(
    task=ligand_chemistry_task,
    name="amdock_ligand_chemistry_job",
    params_model=LigandChemistryJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=CHEMISTRY_GRAPH_OUTPUT,
    output_flush_every=1,
    store_results=False,
)
def ligand_chemistry_job(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
    parsed = LigandChemistryJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("ligand_chemistry_job requires project_db in config.")
    yield from _iter_ligand_chemistry_batches(
        project_db=project_db,
        db_path=project_db_path(project_db),
        output_dir=resolve_ligand_storage_dir(config_map),
        params=parsed,
    )


@job(
    task=receptor_chemistry_task,
    name="amdock_receptor_chemistry_job",
    params_model=ReceptorChemistryJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=CHEMISTRY_GRAPH_OUTPUT,
    output_flush_every=1,
    store_results=False,
)
def receptor_chemistry_job(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
    parsed = ReceptorChemistryJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("receptor_chemistry_job requires project_db in config.")
    yield from _iter_receptor_chemistry_batches(
        project_db=project_db,
        db_path=project_db_path(project_db),
        output_dir=resolve_receptor_storage_dir(config_map),
        params=parsed,
    )


__all__ = [
    "LigandChemistryJobParams",
    "ReceptorChemistryJobParams",
    "ligand_chemistry_job",
    "ligand_chemistry_task",
    "receptor_chemistry_job",
    "receptor_chemistry_task",
]
