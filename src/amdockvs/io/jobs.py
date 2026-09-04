from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field

from ms_flow.sinks import graph_sink, table_sink
from ms_flow.specs import InputSource
from ms_flow.tasking import job, task

from amdockvs.constants import (
    AMDOCKVS_LOCAL_EXECUTORS,
    AMDOCKVS_PROCESS_EXECUTORS,
    DEFAULT_LOAD_BATCH_SIZE,
    OUTPUT_FLUSH_EVERY,
    RESOURCE_MOLECULES,
    RESOURCE_SHARDS,
)
from amdockvs.io.loaders import estimate_record_chunks, stream_import_payload_batches
from amdockvs.io.parsers import count_import_records
from amdockvs.io.payloads import ImportPrefilterPolicy, MultithreadedSDFImportPayload
from amdockvs.io.shards import write_ligand_shard
from amdockvs.io.transformers import (
    build_import_graph_payload,
    materialize_import_batch,
    materialize_multithreaded_sdf_file,
    offload_source_properties,
)
from amdockvs.api_common import worker_output_dir
from amdockvs.models import (
    BindingSite,
    ComplexRecord,
    LigandActivity,
    MoleculeModel,
    MoleculeRecord,
    MoleculeSet,
    MoleculeSetMember,
    MoleculeSourceProperty,
    ScreeningShard,
)


IMPORT_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {"name": "molecules", "model": MoleculeRecord, "validate_model": False},
        {
            "name": "molecule_models",
            "model": MoleculeModel,
            "validate_model": False,
        },
        {
            "name": "molecule_source_properties",
            "model": MoleculeSourceProperty,
            "validate_model": False,
        },
        {"name": "complexes", "model": ComplexRecord, "validate_model": False},
        {"name": "molecule_sets", "model": MoleculeSet, "validate_model": False},
        {
            "name": "molecule_set_members",
            "model": MoleculeSetMember,
            "validate_model": False,
        },
        {"name": "ligand_activities", "model": LigandActivity, "validate_model": False},
        {"name": "binding_sites", "model": BindingSite, "validate_model": False},
    ),
    relations=(
        {
            "source_node": "molecule_models",
            "source_ref_field": "molecule_ref",
            "target_node": "molecules",
            "fk_field": "molecule_id",
        },
        {
            "source_node": "molecule_source_properties",
            "source_ref_field": "molecule_ref",
            "target_node": "molecules",
            "fk_field": "molecule_id",
        },
        {
            "source_node": "binding_sites",
            "source_ref_field": "molecule_ref",
            "target_node": "molecules",
            "fk_field": "molecule_id",
        },
        {
            "source_node": "complexes",
            "source_ref_field": "binding_site_ref",
            "target_node": "binding_sites",
            "fk_field": "binding_site_id",
        },
        {
            # Deferred: the receptor points at one of its own sites, which are inserted later
            # (they need its molecule_id). The sink closes it with an UPDATE at end of commit.
            "source_node": "molecules",
            "source_ref_field": "active_binding_site_ref",
            "target_node": "binding_sites",
            "fk_field": "active_binding_site_id",
            "deferred": True,
        },
        {
            "source_node": "complexes",
            "source_ref_field": "receptor_ref",
            "target_node": "molecules",
            "fk_field": "receptor_molecule_id",
        },
        {
            "source_node": "complexes",
            "source_ref_field": "ligand_ref",
            "target_node": "molecules",
            "fk_field": "ligand_molecule_id",
        },
        {
            "source_node": "complexes",
            "source_ref_field": "activity_ref",
            "target_node": "ligand_activities",
            "fk_field": "activity_id",
        },
        {
            "source_node": "molecule_set_members",
            "source_ref_field": "molecule_ref",
            "target_node": "molecules",
            "fk_field": "molecule_id",
        },
        {
            "source_node": "molecule_set_members",
            "source_ref_field": "set_ref",
            "target_node": "molecule_sets",
            "fk_field": "set_id",
        },
        {
            "source_node": "ligand_activities",
            "source_ref_field": "molecule_ref",
            "target_node": "molecules",
            "fk_field": "molecule_id",
        },
    ),
)


class LoadFileParams(BaseModel):
    file_path: Path | None = None
    file_paths: list[Path] = Field(default_factory=list)
    storage_dir: Path | None = None
    storage_resource: str | None = None
    batch_size: int = Field(default=DEFAULT_LOAD_BATCH_SIZE, ge=1)
    primary_role: str = ""
    primary_context: str = "general"
    molecule_kind: str = "unknown"
    prefilter: ImportPrefilterPolicy | None = None
    extra_data_patch: dict[str, Any] = Field(default_factory=dict)
    binding_site_specs: list[dict[str, Any]] = Field(default_factory=list)
    extra_data_patch_by_file: dict[str, dict[str, Any]] = Field(default_factory=dict)
    binding_site_specs_by_file: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class LoadMultithreadedSDFParams(BaseModel):
    file_paths: list[Path] = Field(min_length=1)
    storage_dir: Path | None = None
    storage_resource: str | None = None
    num_threads: int = Field(default=4, ge=1)
    primary_role: str = "ligand"
    primary_context: str = "general"
    molecule_kind: str = "small_molecule"
    prefilter: ImportPrefilterPolicy | None = None


def _resolve_storage_dir(*, kind: str, params: Mapping[str, Any], config: Mapping[str, Any] | None) -> Path:
    if raw_storage_dir := params.get("storage_dir"):
        return Path(raw_storage_dir).expanduser().resolve()

    config_map = dict(config or {})
    resources = dict(config_map.get("project_resources") or {})
    resource_key = str(params.get("storage_resource") or "").strip().lower()
    if not resource_key:
        resource_key = RESOURCE_MOLECULES
    resource = dict(resources.get(resource_key) or {})
    if path_text := str(resource.get("path") or "").strip():
        return Path(path_text).expanduser().resolve()
    raise ValueError(
        f"Missing project resource '{resource_key}' for import job. "
        "Submit the job through AMDock runtime with an active project."
    )


@dataclass(frozen=True)
class FileInput(InputSource):
    kind: str = ""

    def iter_items(self, params: dict[str, Any], config: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise NotImplementedError("FileInput uses iter_chunks() directly.")

    def iter_chunks(
        self,
        params: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        parsed = LoadFileParams.model_validate(dict(params or {}))
        params_map = parsed.model_dump(mode="python")
        storage_dir = _resolve_storage_dir(kind=self.kind, params=params_map, config=config)
        file_paths = list(parsed.file_paths or [])
        if parsed.file_path is not None:
            file_paths.append(parsed.file_path)
        if not file_paths:
            raise ValueError("Missing required parameter: file_path or file_paths")
        patch_by_file = {
            str(Path(path).expanduser().resolve()): dict(value or {})
            for path, value in dict(parsed.extra_data_patch_by_file or {}).items()
        }
        binding_by_file = {
            str(Path(path).expanduser().resolve()): [dict(item) for item in list(value or [])]
            for path, value in dict(parsed.binding_site_specs_by_file or {}).items()
        }
        for file_path in file_paths:
            resolved_file = Path(file_path).expanduser().resolve()
            for chunk in stream_import_payload_batches(
                kind=self.kind,
                file_path=resolved_file,
                storage_dir=storage_dir,
                batch_size=int(parsed.batch_size or self.batch_size),
                primary_role=parsed.primary_role,
                primary_context=parsed.primary_context,
                molecule_kind=parsed.molecule_kind,
                prefilter=parsed.prefilter,
                extra_data_patch=patch_by_file.get(str(resolved_file), parsed.extra_data_patch),
                binding_site_specs=binding_by_file.get(str(resolved_file), parsed.binding_site_specs),
            ):
                chunk["storage_dir"] = worker_output_dir(storage_dir)
                yield chunk


def iter_import_chunks(kind: str, params: dict[str, Any], config: dict[str, Any] | None = None) -> Iterator[dict]:
    yield from FileInput(kind=kind, batch_size=DEFAULT_LOAD_BATCH_SIZE).iter_chunks(
        params=params,
        config=config or {},
    )


# A shard is a unit of work for one CPU on one machine, so it is much bigger than an import
# batch: ~10k molecules is a few seconds of docking prep, not a few milliseconds of parsing.
DEFAULT_SHARD_SIZE = int(os.environ.get("AMDOCK_SHARD_SIZE", "10000"))
# Above this much input, importing as rows is almost certainly a mistake: the importer offers
# shards instead. ponytail: bytes on disk, not a record count — one stat() per file against
# parsing 40 GB to answer a question the size already answers.
SHARD_SUGGEST_BYTES = int(os.environ.get("AMDOCK_SHARD_SUGGEST_BYTES", str(200 * 1024 * 1024)))

# Offload SDF tags to parquet sidecars instead of persisting ~34 rows/mol into the
# project DB. Env-toggle so it can be A/B'd; default on (props are load-on-demand).
OFFLOAD_IMPORT_PROPERTIES = os.environ.get("AMDOCK_OFFLOAD_PROPS", "1") == "1"


@task(
    name="amdock_materialize_import_batch",
    description="Parse a raw import batch and materialize a graph payload for project storage.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def materialize_import_rows(payload: dict, progress_cb=None):
    rows = materialize_import_batch(payload, progress_cb=progress_cb)
    if OFFLOAD_IMPORT_PROPERTIES:
        offload_source_properties(rows, payload.get("storage_dir"))
    return build_import_graph_payload(rows)


@task(
    name="amdock_materialize_multithreaded_sdf_file",
    description="Parse a full SDF file with RDKit MultithreadedSDMolSupplier and materialize a graph payload.",
    executor="compute",
    supported_executors=AMDOCKVS_PROCESS_EXECUTORS,
    cpu_required=4,
)
def materialize_multithreaded_sdf_rows(payload: dict, progress_cb=None):
    rows = materialize_multithreaded_sdf_file(payload, progress_cb=progress_cb)
    return build_import_graph_payload(rows)


@job(
    task=materialize_import_rows,
    name="amdock_load_molecules_file_job",
    params_model=LoadFileParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    # feed_mode="durable_feed",
    output_spec=IMPORT_GRAPH_OUTPUT,
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def load_molecules_file_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    yield from iter_import_chunks("molecule", params=params, config=config)


@job(
    task=materialize_import_rows,
    name="amdock_load_ligands_file_job",
    params_model=LoadFileParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    # feed_mode="durable_feed",
    output_spec=IMPORT_GRAPH_OUTPUT,
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def load_ligands_file_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    params_map = {
        **dict(params or {}),
        "primary_role": str(dict(params or {}).get("primary_role") or "ligand"),
        "primary_context": str(dict(params or {}).get("primary_context") or "general"),
        "molecule_kind": str(dict(params or {}).get("molecule_kind") or "small_molecule"),
        "storage_resource": str(dict(params or {}).get("storage_resource") or RESOURCE_MOLECULES),
    }
    yield from iter_import_chunks("ligand", params=params_map, config=config)


@job(
    task=materialize_import_rows,
    name="amdock_load_receptors_file_job",
    params_model=LoadFileParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    # feed_mode="durable_feed",
    output_spec=IMPORT_GRAPH_OUTPUT,
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def load_receptors_file_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    params_map = {
        **dict(params or {}),
        "primary_role": str(dict(params or {}).get("primary_role") or "receptor"),
        "primary_context": str(dict(params or {}).get("primary_context") or "general"),
        "molecule_kind": str(dict(params or {}).get("molecule_kind") or "protein"),
        "storage_resource": str(dict(params or {}).get("storage_resource") or RESOURCE_MOLECULES),
    }
    yield from iter_import_chunks("receptor", params=params_map, config=config)


@job(
    task=materialize_multithreaded_sdf_rows,
    name="amdock_load_ligands_multithreaded_sdf_job",
    params_model=LoadMultithreadedSDFParams,
    executor="compute",
    supported_executors=AMDOCKVS_PROCESS_EXECUTORS,
    cpu_required=4,
    # feed_mode="durable_feed",
    output_spec=IMPORT_GRAPH_OUTPUT,
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def load_ligands_multithreaded_sdf_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    parsed = LoadMultithreadedSDFParams.model_validate(dict(params or {}))
    params_map = parsed.model_dump(mode="python")
    storage_dir = _resolve_storage_dir(kind="ligand", params=params_map, config=config or {})
    for file_path in parsed.file_paths:
        resolved_file = Path(file_path).expanduser().resolve()
        if resolved_file.suffix.lower() != ".sdf":
            raise ValueError("load_ligands_multithreaded_sdf_job only supports .sdf files.")
        payload = MultithreadedSDFImportPayload(
            kind="ligand",
            file_path=resolved_file,
            storage_dir=storage_dir,
            num_threads=parsed.num_threads,
            primary_role=str(parsed.primary_role or "ligand"),
            primary_context=str(parsed.primary_context or "general"),
            molecule_kind=str(parsed.molecule_kind or "small_molecule"),
            prefilter=parsed.prefilter,
        ).model_dump(mode="json")
        payload["storage_dir"] = worker_output_dir(storage_dir)
        yield payload


class ShardLigandsParams(LoadFileParams):
    """The import params, with the batch size read as *records per shard*."""

    batch_size: int = Field(default=DEFAULT_SHARD_SIZE, ge=1)


@task(
    name="amdock_write_ligand_shard",
    description="Write one span of a source library out as a shard file.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def write_ligand_shard_task(payload: dict, progress_cb=None) -> list[dict]:
    return [write_ligand_shard(payload)]


@job(
    task=write_ligand_shard_task,
    name="amdock_shard_ligands_job",
    params_model=ShardLigandsParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    # Upsert on (source, shard_index): re-importing the same file rewrites its shards instead
    # of duplicating the inventory.
    output_spec=table_sink(
        model=ScreeningShard, write_mode="upsert", conflict_keys=("source", "shard_index")
    ),
    output_flush_every=OUTPUT_FLUSH_EVERY,
    store_results=False,
)
def shard_ligands_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """`htpvs` import: the library is split, not materialized. `molecules` is never touched."""
    params_map = {
        **dict(params or {}),
        "primary_role": "ligand",
        "molecule_kind": str(dict(params or {}).get("molecule_kind") or "small_molecule"),
    }
    shard_dir = _resolve_storage_dir(
        kind="ligand", params={"storage_resource": RESOURCE_SHARDS}, config=config or {}
    )
    for shard_index, chunk in enumerate(iter_import_chunks("ligand", params=params_map, config=config)):
        # The chunk is already a byte span of the source; these two fields turn it into a shard.
        chunk["shard_dir"] = worker_output_dir(shard_dir)
        chunk["shard_index"] = shard_index
        yield chunk


def estimate_import_chunks(file_path: str | Path, *, batch_size: int) -> int:
    # approx=True: this only feeds the progress bar's declared total, and it runs on the GUI
    # thread at submit time — an exact scan of a big library froze the UI for seconds.
    expected_records = count_import_records(file_path, approx=True)
    return estimate_record_chunks(expected_records, batch_size)
