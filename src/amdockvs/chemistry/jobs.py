from __future__ import annotations

from datetime import datetime
from itertools import batched
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.sinks import graph_sink, table_sink
from ms_flow.tasking import job, task

from amdockvs.constants import AMDOCKVS_LOCAL_EXECUTORS, OUTPUT_FLUSH_EVERY
from amdockvs.molecules.store import ShardStore, shard_scope_spec, store_from_config
from amdockvs.api_common import (
    project_root_from_output_dir,
    restore_worker_paths,
    worker_file,
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
from amdockvs.chemistry.pipeline import normalize_steps
from amdockvs.chemistry.service import transform_ligand_rows, transform_receptor_rows
from amdockvs.chemistry.shards import transform_ligand_shard
from amdockvs.models import MoleculeModel, MoleculeRecord, ScreeningShard
from amdockvs.molecule_paths import set_default_project_root
from amdockvs.vocab import ShardState


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
    # A bare name, or a step list `[["standardize", {}], ["protonate", {"ph": 7.4}]]`. The
    # service normalizes both, so a one-step call stays `operation="protonate"`.
    operation: str | list[Any]
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
    store=None,
) -> Iterator[dict[str, Any]]:
    return _chemistry_batches(
        project_db=project_db,
        db_path=db_path,
        output_dir=output_dir,
        params=params,
        rows=iter_ligand_rows(
            store or project_db,
            ligand_set_id=params.ligand_set_id,
            filters=params.ligand_filters,
            batch_size=params.batch_size,
        ),
        gpu=any(name == "protonate" for name, _ in normalize_steps(params.operation))
        and bool(params.params.get("gpu")),
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
        operations=payload.get("operation") or "",
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
    output_flush_every=OUTPUT_FLUSH_EVERY,
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
        store=store_from_config(config_map),
    )


@job(
    task=receptor_chemistry_task,
    name="amdock_receptor_chemistry_job",
    params_model=ReceptorChemistryJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=CHEMISTRY_GRAPH_OUTPUT,
    output_flush_every=OUTPUT_FLUSH_EVERY,
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


class ShardChemistryJobParams(BaseModel):
    """The same step list as the row pipeline, run over shards instead of rows."""

    operation: str | list[Any]
    params: dict[str, Any] = Field(default_factory=dict)
    # Which shards to feed. `pending` is the idempotent default: a shard that finished is
    # already `done`, so re-running the job after a crash resumes instead of recomputing.
    state: str = ShardState.PENDING


@task(
    name="amdock_shard_chemistry",
    description="Run the ligand chemistry pipeline over one shard file.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def shard_chemistry_task(payload: dict, progress_cb=None) -> list[dict]:
    shard_path = Path(str(payload["shard_path"])).expanduser().resolve()
    output_path = Path(str(payload["output_dir"])).expanduser().resolve() / shard_path.name
    stats = transform_ligand_shard(shard_path, output_path, payload["steps"])
    # A shard whose molecules all failed is still a done shard: it holds zero records now.
    return [{
        "source": payload["source"],
        "shard_index": int(payload["shard_index"]),
        "path": str(output_path),
        "n_records": int(stats["n_records"]),
        "state": ShardState.DONE,
        "error": "" if not stats["n_failed"] else f"{stats['n_failed']} of {stats['n_input']} molecules failed",
        "updated_at": datetime.now(),
    }]


@job(
    task=shard_chemistry_task,
    name="amdock_shard_chemistry_job",
    params_model=ShardChemistryJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=table_sink(
        model=ScreeningShard, write_mode="upsert", conflict_keys=("source", "shard_index")
    ),
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def shard_chemistry_job(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
    """One shard per chunk: one file in, one file out, one CPU."""
    parsed = ShardChemistryJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("shard_chemistry_job requires project_db in config.")
    steps = normalize_steps(parsed.operation)
    label = "+".join(name for name, _ in steps)
    resolved_steps = [(name, {**dict(parsed.params or {}), **step_params}) for name, step_params in steps]
    for row in ShardStore(project_db).iter_rows(shard_scope_spec(state=parsed.state)):
        shard_path = Path(str(row.get("path") or ""))
        yield {
            "shard_path": worker_file(shard_path),
            # Beside the input, in a folder named after the pipeline: the shard the docking
            # reads is whatever `path` points at now, and the original is still there.
            "output_dir": worker_output_dir(shard_path.parent / label),
            "source": str(row.get("source") or ""),
            "shard_index": int(row.get("shard_index") or 0),
            "steps": resolved_steps,
        }


__all__ = [
    "LigandChemistryJobParams",
    "ReceptorChemistryJobParams",
    "ligand_chemistry_job",
    "ligand_chemistry_task",
    "receptor_chemistry_job",
    "receptor_chemistry_task",
    "ShardChemistryJobParams",
    "shard_chemistry_job",
    "shard_chemistry_task",
]
