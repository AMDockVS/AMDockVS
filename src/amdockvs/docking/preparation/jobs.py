from __future__ import annotations

from itertools import batched
from pathlib import Path
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec
from ms_flow.sinks import table_sink
from amdockvs.core.jobs import JobSpec
from amdockvs.docking.preparation.tasks import prepare_entities, prepare_ligand_shards

from amdockvs.core.configuration import DEFAULT_OUTPUT_FLUSH_EVERY, batch_size_for
from amdockvs.core.payloads import molecule_payload_id, payload_store
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.docking.repository import (
    iter_entity_rows,
    molecule_scope_spec,
)
from amdockvs.docking.preparation.shards import PREPARED_DIR
from amdockvs.models import EngineState, ShardEngineState
from amdockvs.molecules.storage import ShardStore, shard_scope_spec


ENGINE_STATE_UPSERT = table_sink(
    model=EngineState,
    write_mode="upsert",
    conflict_keys=("molecule_id", "role_type", "engine"),
)


class PreparationJobParams(BaseModel):
    # None -> settings (batch_sizes.ligand / batch_sizes.receptor), resolved at chunk-build time.
    batch_size: int | None = Field(default=None, ge=1)
    ligand_set_id: int | None = Field(default=None, ge=1)
    receptor_set_id: int | None = Field(default=None, ge=1)
    ligand_filters: dict[str, Any] = Field(default_factory=dict)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)
    engine: str = "ad4"
    force: bool = False
    # Receptors only: what import kept is not automatically what gets docked. Off by default,
    # so a receptor prepared today is the same dry structure it was before these existed.
    keep_waters: bool = False
    keep_cofactors: bool = False


def scope_spec(params: PreparationJobParams, *, entity_kind: str) -> QuerySpec:
    """The scope of a preparation, fully declared: the same WHERE that feeds the feed.

    `db_count` over this spec gives the ceiling of the scope: with `force=False` the feed also
    drops rows already prepared, which this spec does not say. That is why this job does not
    declare `total_chunks` — over-declaring would leave it never completing.
    """
    return molecule_scope_spec(
        entity_kind=entity_kind,
        engine=params.engine,
        set_id=params.ligand_set_id if entity_kind == "ligand" else params.receptor_set_id,
        filters=params.ligand_filters if entity_kind == "ligand" else params.receptor_filters,
        fields=("id", "stored_payload_id", "current_payload_id", "has_3d"),
        order=("id",),
    )


def _rows_for_preparation(
    *,
    project_db,
    entity_kind: str,
    engine: str,
    set_id: int | None,
    filters: Mapping[str, Any] | None,
    force: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    rows = iter_entity_rows(
        project_db,
        entity_kind=entity_kind,
        engine=engine,
        set_id=set_id,
        filters=filters,
        fields=("id", "stored_payload_id", "current_payload_id", "has_3d", "prepared_payload_id"),
        order=("id",),
        batch_size=batch_size,
    )
    if force:
        return rows

    return (row for row in rows if not row.get("prepared_payload_id"))


def _attach_flexible_residues(project_db, rows: list[dict[str, Any]]) -> None:
    from amdockvs.models import MoleculeRecord

    with project_db.get_session() as session:
        for row in rows:
            rec = session.get(MoleculeRecord, int(row.get("id") or 0))
            if rec is None:
                continue
            keys = list((dict(rec.extra_data or {}).get("flexible_residues") or []))
            if keys:
                row["flexible_residues"] = [str(k) for k in keys]


def _with_structures(project_db, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each row with the bytes it is prepared from (`structure`, `structure_kind`), read in disk order."""
    for row in rows:
        row["structure_payload_id"] = molecule_payload_id(row)
    ids = {row["structure_payload_id"] for row in rows} - {None}
    store = payload_store(project_db)
    found, kinds = (store.get_many(ids), store.kinds(ids)) if ids else ({}, {})
    for row in rows:
        row["structure"] = found.get(row["structure_payload_id"])
        row["structure_kind"] = kinds.get(row["structure_payload_id"], "")
    return rows


def _iter_preparation_batches(
    *,
    project_db,
    entity_kind: str,
    params: PreparationJobParams,
) -> Iterator[dict[str, Any]]:
    set_id = params.ligand_set_id if entity_kind == "ligand" else params.receptor_set_id
    filters = params.ligand_filters if entity_kind == "ligand" else params.receptor_filters
    batch_size = max(1, int(params.batch_size or batch_size_for(entity_kind)))
    rows_iter = _rows_for_preparation(
        project_db=project_db,
        entity_kind=entity_kind,
        engine=params.engine,
        set_id=set_id,
        filters=filters,
        force=bool(params.force),
        batch_size=batch_size,
    )

    def chunk(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "entity_kind": entity_kind,
            "engine": params.engine,
            "rows": _with_structures(project_db, rows),
            "keep_waters": bool(params.keep_waters),
            "keep_cofactors": bool(params.keep_cofactors),
        }

    emitted = False
    for batch in batched(rows_iter, batch_size):
        rows = list(batch)
        if entity_kind == "receptor":
            # Flexible residues live on the receptor's extra_data; carry them into the worker payload
            # (the row fields don't include extra_data). Receptors are few, so per-row lookup is fine.
            _attach_flexible_residues(project_db, rows)
        emitted = True
        yield chunk(rows)
    if not emitted:
        # MF still needs one chunk to close the job out cleanly when the scope is empty.
        yield chunk([])


def _build_preparation_chunks(
    *,
    params: dict,
    config: dict | None,
    entity_kind: str,
) -> Iterator[dict[str, Any]]:
    parsed = PreparationJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError(f"prepare_{entity_kind}s_job requires project_db in config.")
    yield from _iter_preparation_batches(
        project_db=project_db,
        entity_kind=entity_kind,
        params=parsed,
    )


class _PrepareEntitiesJobSpec(JobSpec):
    description = "Prepare ligands or receptors with a registered profile and persist EngineState rows."
    params_model = PreparationJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_OFFLOADABLE_EXECUTORS
    output_spec = ENGINE_STATE_UPSERT
    output_flush_every = DEFAULT_OUTPUT_FLUSH_EVERY
    store_results = False

    run_chunk = staticmethod(prepare_entities)


class PrepareLigandsJobSpec(_PrepareEntitiesJobSpec):
    name = "amdock_prepare_ligands_job"
    task_name = "amdock_prepare_ligands_task"
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        yield from _build_preparation_chunks(params=params, config=config, entity_kind="ligand")


class PrepareReceptorsJobSpec(_PrepareEntitiesJobSpec):
    name = "amdock_prepare_receptors_job"
    task_name = "amdock_prepare_receptors_task"
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        yield from _build_preparation_chunks(params=params, config=config, entity_kind="receptor")


class PrepareLigandShardsJobParams(BaseModel):
    """The sharded twin of `PreparationJobParams`: no set, filters, or sub-batching."""

    engine: str = "ad4"
    force: bool = False


class PrepareLigandShardsJobSpec(JobSpec):
    """Whole shards per chunk: molecules in, PDBQT text out, both inside `.mshard` containers."""

    name = "amdock_prepare_ligand_shards_job"
    task_name = "amdock_prepare_ligand_shards_task"
    description = "Prepare a sharded screening library with a registered profile."
    params_model = PrepareLigandShardsJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_OFFLOADABLE_EXECUTORS
    output_spec = table_sink(
        model=ShardEngineState, write_mode="upsert", conflict_keys=("shard_id", "engine")
    )
    output_flush_every = DEFAULT_OUTPUT_FLUSH_EVERY
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        parsed = PrepareLigandShardsJobParams(**params)
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("prepare_ligand_shards_job requires project_db in config.")

        from sqlmodel import select

        with project_db.get_session() as session:
            prepared_states = {
                int(row.shard_id): row
                for row in session.exec(
                    select(ShardEngineState).where(ShardEngineState.engine == parsed.engine)
                ).all()
            }

        def pending() -> Iterator[dict[str, Any]]:
            for row in ShardStore(project_db).iter_rows(shard_scope_spec(state=None)):
                shard_path = Path(str(row.get("path") or ""))
                shard_id = int(row.get("id") or 0)
                state = prepared_states.get(shard_id)
                prepared_path = str(dict(state.files or {}).get("prepared") or "") if state else ""
                prepared = bool(state and state.is_ready and prepared_path and Path(prepared_path).is_file())
                # Compatibility with projects prepared before engine artifacts had their own
                # table. A forced preparation writes the new state and completes the migration.
                prepared = prepared or (
                    parsed.engine == "ad4"
                    and str(row.get("input_format") or "").lower() == "pdbqt"
                    and shard_path.is_file()
                )
                if prepared and not parsed.force:
                    continue
                yield {
                    "shard_id": shard_id,
                    "shard_path": worker_file(shard_path),
                    # Beside the input, in a folder named after the engine — same shape as the
                    # chemistry pass, so a campaign's stages read as a chain of folders.
                    "output_dir": worker_output_dir(
                        shard_path.parent / PREPARED_DIR.format(engine=parsed.engine)
                    ),
                    "source": str(row.get("source") or ""),
                    "shard_index": int(row.get("shard_index") or 0),
                }

        emitted = False
        for shard in pending():
            emitted = True
            yield {"shards": [shard], "engine": parsed.engine}
        if not emitted:
            # MF still needs one chunk to close the job out cleanly when nothing is pending.
            yield {"shards": [], "engine": parsed.engine}

    run_chunk = staticmethod(prepare_ligand_shards)


prepare_ligands_job = PrepareLigandsJobSpec.to_job_definition()
prepare_receptors_job = PrepareReceptorsJobSpec.to_job_definition()
prepare_ligand_shards_job = PrepareLigandShardsJobSpec.to_job_definition()


__all__ = [
    "PreparationJobParams",
    "PrepareLigandShardsJobParams",
    "PrepareLigandShardsJobSpec",
    "prepare_ligand_shards_job",
    "PrepareLigandsJobSpec",
    "PrepareReceptorsJobSpec",
    "prepare_ligands_job",
    "prepare_receptors_job",
]
