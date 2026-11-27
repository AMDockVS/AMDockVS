"""Repacking the library after a strong cut (M12b): a job whose tasks re-encode what the cuts
leave into fuller shards of a new generation, every ligand id kept (`ms_flow.core.data.repack`).

The generation is opened at submit, so a resumed job fills the same one (trap 17); it becomes the
library once the job completes, the previous one kept. A canceled or failed repack leaves its
generation out: it is deleted, and the library is as it was.
"""
from __future__ import annotations

from typing import Any, Iterator

from pydantic import BaseModel, Field
from ms_flow.sinks import catalog_sink

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.core.jobs import JobSpec
from amdockvs.core.worker_io import worker_file, worker_output_dir


class RepackLibraryParams(BaseModel):
    generation_id: int = Field(ge=1)  # the one `repack.start` opened at submit
    records: int = Field(ge=1)  # ligands a repacked shard holds at most


def repack_shards(element: dict, progress=None) -> list[dict]:
    """A task: one run of neighbouring shards as one part of the new generation; its receipt."""
    from ms_flow.core.data import repack

    return [repack.part(element)] if element.get("shards") else []


class RepackLibraryJobSpec(JobSpec):
    name = "amdock_repack_library_job"
    task_name = "amdock_repack_library_task"
    description = "Repack the screening library into fuller shards after a strong filter."
    params_model = RepackLibraryParams
    executor = "compute"
    supported_executors = AMDOCKVS_OFFLOADABLE_EXECUTORS
    output_spec = catalog_sink()
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        from ms_flow.core.data import repack

        parsed = RepackLibraryParams(**params)
        project_db = dict(config or {}).get("project_db")
        if project_db is None:
            raise ValueError("repack_library_job requires project_db in config.")
        emitted = False
        for element in repack.elements(project_db, parsed.generation_id, records=parsed.records):
            emitted = True
            yield {**element, "out": worker_output_dir(element["out"]),
                   "shards": [{**shard, "path": worker_file(shard["path"])} for shard in element["shards"]]}
        if not emitted:  # every ligand cut: one chunk still closes the job out
            yield {"shards": []}

    @staticmethod
    def finalize(params: dict, config: dict) -> None:
        from ms_flow.core.data import repack

        parsed = RepackLibraryParams(**params)
        project_db, status = config["project_db"], str(config.get("terminal_status") or "")
        if status.startswith("completed"):
            repack.activate(project_db, parsed.generation_id)
        elif status in ("canceled", "failed"):  # interrupted: it goes on when resumed
            repack.delete(project_db, parsed.generation_id)

    run_chunk = staticmethod(repack_shards)


repack_library_job = RepackLibraryJobSpec.to_job_definition()

__all__ = ["RepackLibraryJobSpec", "RepackLibraryParams", "repack_library_job", "repack_shards"]
