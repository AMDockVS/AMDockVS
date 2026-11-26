"""No job writes rows through a path of its own: it either declares a sink or is whitelisted.

Without this, a new job can persist from `finalize` with its own session and nobody notices —
which is exactly how `amdock_pocket_prediction_job` ended up before it became additive. The test
does not guess whether a job writes: it forces every exception to be explicit and argued here.
"""
from __future__ import annotations

import importlib
import pkgutil

import amdockvs
from amdockvs.core.jobs import JobDefinition

# Why each one has no sink. If one gains one, this test fails and its line must be deleted.
WITHOUT_SINK = {
    "amdock_diagram_job": "writes no rows: it leaves PNG/SVG on disk.",
    "amdock_shard_ligands_job": (
        "declares a result_handler_factory instead: ShardImportWriter commits the parts workers "
        "wrote to the shard catalog, then counts each batch from it. A table sink cannot commit "
        "a container part."
    ),
    "amdock_shard_chemistry_job": (
        "declares a result_handler_factory instead: ShardGenerationWriter writes the rows into "
        "a generation nothing reads yet and flips the active pointer once, at the end. The "
        "upsert-on-(source, shard_index) sink it replaced repointed the inventory one chunk at "
        "a time, so a job that died mid-run left half the library on the new step."
    ),
    "amdock_dock_shards_job": (
        "declares a result_handler_factory instead: ShardHitWriter holds the hit gate, which "
        "counts across chunks and decides which scored ligands become molecules at all. A sink "
        "would write every pose back, which is what the sharded mode exists to prevent."
    ),
    "amdock_shard_fingerprints_job": (
        "writes no rows: each shard's fingerprints go to .npy files the clustering job that "
        "depends on it reads and deletes."
    ),
    "amdock_shard_top_n_job": (
        "declares a result_handler_factory instead: TopNWriter ingests the ranked hits a worker "
        "read and compressed; the rows go through the docking ingest, not a table sink."
    ),
    "amdock_promote_records_job": (
        "declares a result_handler_factory instead: PromotionWriter inserts the molecules a worker "
        "parsed, once each by (batch, record id), and the clustering rows of the ones a run lacks."
    ),
    "amdock_pack_rows_job": (
        "writes no rows: workers pack transport shards on disk; the handler only lists their "
        "names for the campaign manifest."
    ),
    "amdock_qsar_predict_shards_job": (
        "writes no rows: the scores stay in a parquet per shard, by reference, until "
        "promote_shard_predictions turns a cut of them into rows."
    ),
}


def _job_definitions() -> dict[str, JobDefinition]:
    found: dict[str, JobDefinition] = {}
    for module in pkgutil.walk_packages(amdockvs.__path__, "amdockvs."):
        imported = importlib.import_module(module.name)
        for value in vars(imported).values():
            if isinstance(value, JobDefinition):
                found[value.name] = value
    return found


def test_every_job_declares_a_sink_or_is_whitelisted():
    jobs = _job_definitions()
    assert jobs, "no JobDefinition was found: the module walk is broken."
    assert {name for name, job in jobs.items() if job.output_spec is None} == set(WITHOUT_SINK)
