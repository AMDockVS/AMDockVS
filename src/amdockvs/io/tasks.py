"""What a worker runs to import: parse a batch of a file into rows, or filter a span of it into a shard.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

import os

from amdockvs.core.constants import AMDOCKVS_LOCAL_EXECUTORS, AMDOCKVS_PROCESS_EXECUTORS
from amdockvs.core.jobs import task
from amdockvs.io.shards import filter_ligand_span
from amdockvs.io.transformers import (
    build_import_graph_payload,
    materialize_import_batch,
    materialize_multithreaded_sdf_file,
    offload_source_properties,
)


# Offload SDF tags to one parquet payload per batch instead of persisting ~34 rows/mol into the
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
        offload_source_properties(rows)
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
    if OFFLOAD_IMPORT_PROPERTIES:
        offload_source_properties(rows)
    return build_import_graph_payload(rows)


@task(
    name="amdock_filter_ligand_span",
    description="Filter one span of a source library; the survivors go back to the shard queue.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def filter_ligand_span_task(payload: dict, progress_cb=None) -> list[dict]:
    return filter_ligand_span(payload)
