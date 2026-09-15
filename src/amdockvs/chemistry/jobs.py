from __future__ import annotations

from datetime import datetime
from itertools import batched
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import db_pages
from ms_flow.sinks import graph_sink, table_sink
from ms_flow.tasking import job, task

from amdockvs.core.configuration import DEFAULT_OUTPUT_FLUSH_EVERY
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.molecules.storage import ShardStore, shard_scope_spec, store_from_config
from amdockvs.core.worker_io import project_root_from_output_dir, restore_worker_paths, worker_file, worker_output_dir, worker_path_fields
from amdockvs.chemistry.repository import (
    iter_ligand_rows,
    iter_receptor_rows,
    max_model_index_by_molecule_ids,
    project_db_path,
    resolve_ligand_storage_dir,
    resolve_receptor_storage_dir,
    scope_spec,
    sequences_by_molecule_ids,
)
from amdockvs.chemistry.pipeline import normalize_steps
from amdockvs.chemistry.service import transform_ligand_rows, transform_receptor_rows
from amdockvs.chemistry.shards import transform_ligand_shard
from amdockvs.chemistry.tools.esmfold import FAST_MODEL, predict_structure, sequence_from_structure
from amdockvs.io.shards import shard_generation_writer
from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.core.paths import artifact_storage_path, current_molecule_path, molecule_storage_key, set_default_project_root
from amdockvs.core.vocab import ModelSource, ShardState


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
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
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
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
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
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=CHEMISTRY_GRAPH_OUTPUT,
    output_flush_every=DEFAULT_OUTPUT_FLUSH_EVERY,
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
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=CHEMISTRY_GRAPH_OUTPUT,
    output_flush_every=DEFAULT_OUTPUT_FLUSH_EVERY,
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


class ReceptorPredictionJobParams(BaseModel):
    model: str = FAST_MODEL
    force: bool = False
    receptor_set_id: int | None = Field(default=None, ge=1)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)


# Like CHEMISTRY_GRAPH_OUTPUT, plus stored_path: a sequence-only protein's first model is its
# canonical structure, so the prediction fills both paths. extra_data is left untouched.
PREDICTION_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": [
                "id", "stored_path", "current_path", "current_model_index",
                "has_3d", "has_hs", "is_minimized", "conformer_count", "updated_at",
            ],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
        {"name": "molecule_models", "model": MoleculeModel, "validate_model": False},
    ),
)

_PREDICTION_FIELDS = ("id", "source", "source_index", "stored_path", "current_path", "current_model_index", "has_3d")


def prediction_scope_spec(params: ReceptorPredictionJobParams):
    return scope_spec(
        role_flag="is_receptor",
        molecule_set_id=params.receptor_set_id,
        filters=params.receptor_filters,
        fields=_PREDICTION_FIELDS,
        require_structure=False,
    )


@task(
    name="amdock_receptor_prediction",
    description="Predict one protein structure from its sequence with the ESMFold API.",
    executor="thread",
    supported_executors=("thread",),
)
def receptor_prediction_task(payload: dict, progress_cb=None):
    """One protein per chunk: a failed call fails that protein only, not credits spent on others."""
    output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
    project_root = project_root_from_output_dir(output_dir)
    set_default_project_root(project_root)
    row = dict(payload["row"])
    receptor_id = int(row["id"])
    sequence = str(payload.get("sequence") or "")
    if not sequence:
        structure = current_molecule_path(row)
        if structure is None or not structure.is_file():
            raise ValueError(f"Receptor {receptor_id} has neither a sequence nor a structure to read one from.")
        sequence = sequence_from_structure(structure)
    model_index = int(payload["model_index"])
    # variant=id: labelled sources ("user input") repeat across imports, the molecule id does not.
    key = molecule_storage_key("receptor", Path(str(row.get("source") or "receptor")), int(row.get("source_index") or 0), variant=str(receptor_id))
    target = artifact_storage_path(output_dir, role="receptor", key=key, artifact_name=f"esmfold_{model_index}", suffix=".cif")
    metrics, files = predict_structure(sequence, target, model=str(payload["model"]))
    if metrics.get("potential_sequence_of_concern"):
        print(f"ESMFold flagged receptor {receptor_id} as a potential sequence of concern.")
    if progress_cb is not None:
        progress_cb(100.0)

    def relative(path) -> str:
        return str(Path(path).resolve().relative_to(project_root))

    current_path = relative(target)
    return {
        "molecules": [{
            "id": receptor_id,
            "stored_path": str(payload.get("stored_path") or "") or current_path,
            "current_path": current_path,
            "current_model_index": model_index,
            "has_3d": True,
            "has_hs": False,
            "is_minimized": False,
            "conformer_count": 1,
            "updated_at": datetime.now(),
        }],
        "molecule_models": [
            MoleculeModel.build_row(
                molecule_id=receptor_id,
                model_index=model_index,
                file_path=current_path,
                source=ModelSource.ESMFOLD,
                metrics=metrics,
                files={name: relative(path) for name, path in files.items()},
            )
        ],
    }


@job(
    task=receptor_prediction_task,
    name="amdock_receptor_prediction_job",
    params_model=ReceptorPredictionJobParams,
    executor="thread",
    supported_executors=("thread",),
    output_spec=PREDICTION_GRAPH_OUTPUT,
    output_flush_every=1,  # every chunk is paid for: persist it as soon as it lands
    store_results=False,
)
def receptor_prediction_job(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
    parsed = ReceptorPredictionJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("receptor_prediction_job requires project_db in config.")
    output_dir = worker_output_dir(resolve_receptor_storage_dir(config_map))
    for page in batched(db_pages(project_db, prediction_scope_spec(parsed), page_size=32), 32):
        rows = [dict(row) for row in page if parsed.force or not bool(row.get("has_3d"))]
        ids = [int(row["id"]) for row in rows]
        sequences = sequences_by_molecule_ids(project_db, ids)
        max_index = max_model_index_by_molecule_ids(project_db, ids)
        for row in rows:
            receptor_id = int(row["id"])
            yield {
                "row": worker_path_fields(row, "stored_path", "current_path"),
                "stored_path": str(row.get("stored_path") or ""),
                "sequence": sequences.get(receptor_id, ""),
                "model_index": max_index[receptor_id] + 1 if receptor_id in max_index else 0,
                "model": parsed.model,
                "output_dir": output_dir,
            }


class ShardChemistryJobParams(BaseModel):
    """The same step list as the row pipeline, run over shards instead of rows."""

    operation: str | list[Any]
    params: dict[str, Any] = Field(default_factory=dict)
    # Which shards of the active generation to feed; `pending` is every shard of a library
    # nothing has been run over yet, which is what a fresh generation is. Resume-after-crash
    # is not what this does any more: a failed run's generation is discarded whole, so the
    # retry reads the parent from the start.
    state: str = ShardState.PENDING
    # The generation this run writes into, opened by the API before submitting. It is not the
    # active one until the job completes, which is what keeps a half-done rewrite invisible.
    generation_id: int = 0
    output_dir: str = ""


@task(
    name="amdock_shard_chemistry",
    description="Run the ligand chemistry pipeline over one shard file.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
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
        "input_format": payload.get("input_format", ""),
        "n_records": int(stats["n_records"]),
        # Pending, not ready: what a generation's shards have had run over them is the
        # generation's `step`, not a flag on each row. Stamping `ready` here made the next
        # chemistry step (whose feed is `state=pending`) find an empty library.
        "state": ShardState.PENDING,
        "error": "" if not stats["n_failed"] else f"{stats['n_failed']} of {stats['n_input']} molecules failed",
        "updated_at": datetime.now(),
    }]


@job(
    task=shard_chemistry_task,
    name="amdock_shard_chemistry_job",
    params_model=ShardChemistryJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    # Not a table sink: the rows go to a generation that is not the library yet, and the
    # switch happens once at the end. See io/shards.ShardGenerationWriter.
    result_handler_factory=shard_generation_writer,
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
    if any(name == "conformers" for name, _params in steps):
        raise ValueError(
            "Sharded conformer ensembles are not supported by the one-record-per-molecule format."
        )
    resolved_steps = [(name, {**dict(parsed.params or {}), **step_params}) for name, step_params in steps]
    if not parsed.generation_id or not parsed.output_dir:
        raise ValueError("shard_chemistry_job requires a generation to write into.")
    # One directory per generation, so forgetting a generation is deleting a directory.
    output_dir = worker_output_dir(parsed.output_dir)
    for row in ShardStore(project_db).iter_rows(shard_scope_spec(state=parsed.state)):
        shard_path = Path(str(row.get("path") or ""))
        yield {
            "shard_path": worker_file(shard_path),
            "output_dir": output_dir,
            "source": str(row.get("source") or ""),
            "shard_index": int(row.get("shard_index") or 0),
            "input_format": str(row.get("input_format") or ""),
            "steps": resolved_steps,
        }


__all__ = [
    "LigandChemistryJobParams",
    "ReceptorChemistryJobParams",
    "ReceptorPredictionJobParams",
    "prediction_scope_spec",
    "receptor_prediction_job",
    "receptor_prediction_task",
    "ligand_chemistry_job",
    "ligand_chemistry_task",
    "receptor_chemistry_job",
    "receptor_chemistry_task",
    "ShardChemistryJobParams",
    "shard_chemistry_job",
    "shard_chemistry_task",
]
