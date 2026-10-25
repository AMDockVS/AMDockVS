from __future__ import annotations

from itertools import batched
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import db_pages
from ms_flow.sinks import graph_sink, table_sink
from amdockvs.core.jobs import job
from amdockvs.chemistry.tasks import ligand_chemistry_task, receptor_chemistry_task, receptor_prediction_task, shard_chemistry_task

from amdockvs.core.configuration import DEFAULT_OUTPUT_FLUSH_EVERY
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.molecules.storage import ShardStore, shard_scope_spec, store_from_config
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.chemistry.repository import (
    iter_ligand_rows,
    iter_receptor_rows,
    scope_spec,
    sequences_by_molecule_ids,
)
from amdockvs.chemistry.pipeline import normalize_steps
from amdockvs.chemistry.structures import with_structures
from amdockvs.chemistry.tools.esmfold import FAST_MODEL
from amdockvs.io.shards import shard_generation_writer
from amdockvs.models import MoleculeModel, MoleculeRecord
from amdockvs.core.vocab import ShardState


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
                "current_payload_id",
               
            ],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
        {"name": "molecule_models", "model": MoleculeModel, "validate_model": False},
        {
            "name": "molecule_failures",
            "model": MoleculeRecord,
            "columns": ["id", "excluded", "exclusion_reason"],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
    ),
)


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
    params,
    rows: Iterator[dict[str, Any]],
    gpu: bool = False,
) -> Iterator[dict[str, Any]]:
    """One batch of rows = one chunk. `batched` comes from itertools: no hand-rolled accumulator
    and no duplicated final-flush block, which is where the two copies of this came from. Each row
    brings the bytes of the structure it works on, so the task opens no project file."""
    original = str(params.params.get("structure_source") or "current") == "original"
    for batch in batched(with_structures(project_db, rows, original=original), max(1, int(params.batch_size))):
        batch_rows = list(batch)
        yield {
            "operation": params.operation,
            "params": dict(params.params or {}),
            "rows": batch_rows,
            **({"_gpu_required": 1} if gpu else {}),
        }


def _iter_ligand_chemistry_batches(
    *,
    project_db,
    params: LigandChemistryJobParams,
    store=None,
) -> Iterator[dict[str, Any]]:
    return _chemistry_batches(
        project_db=project_db,
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
    params: ReceptorChemistryJobParams,
) -> Iterator[dict[str, Any]]:
    return _chemistry_batches(
        project_db=project_db,
        params=params,
        rows=iter_receptor_rows(
            project_db,
            receptor_set_id=params.receptor_set_id,
            filters=params.receptor_filters,
            batch_size=params.batch_size,
        ),
    )


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
        params=parsed,
    )


class ReceptorPredictionJobParams(BaseModel):
    model: str = FAST_MODEL
    force: bool = False
    receptor_set_id: int | None = Field(default=None, ge=1)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)


# Like CHEMISTRY_GRAPH_OUTPUT, plus stored_payload_id: a sequence-only protein's first model is its
# canonical structure, so the prediction fills both. extra_data is left untouched.
PREDICTION_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": [
                "id", "stored_payload_id", "current_payload_id",
                "has_3d", "has_hs", "is_minimized", "conformer_count",
            ],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
        {"name": "molecule_models", "model": MoleculeModel, "validate_model": False},
    ),
)

_PREDICTION_FIELDS = (
    "id", "source_index", "stored_payload_id", "current_payload_id", "has_3d",
)


def prediction_scope_spec(params: ReceptorPredictionJobParams):
    return scope_spec(
        role_flag="is_receptor",
        molecule_set_id=params.receptor_set_id,
        filters=params.receptor_filters,
        fields=_PREDICTION_FIELDS,
        require_structure=False,
    )


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
    for page in batched(db_pages(project_db, prediction_scope_spec(parsed), page_size=32), 32):
        rows = [dict(row) for row in page if parsed.force or not bool(row.get("has_3d"))]
        ids = [int(row["id"]) for row in rows]
        sequences = sequences_by_molecule_ids(project_db, ids)
        # A forced re-prediction of a structure without a stored sequence reads it from the structure.
        structures = {
            row["id"]: row for row in with_structures(project_db, [r for r in rows if not sequences.get(int(r["id"]))])
        }
        for row in rows:
            receptor_id = int(row["id"])
            known = structures.get(receptor_id, {})
            yield {
                "row": row,
                "sequence": sequences.get(receptor_id, ""),
                "structure": known.get("structure"),
                "structure_kind": known.get("structure_kind"),
                "model": parsed.model,
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
            "import_batch_id": row.get("import_batch_id"),
            "shard_index": int(row.get("shard_index") or 0),
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
