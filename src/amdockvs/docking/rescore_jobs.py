"""Rescoring stored results: a protocol of its own over poses the project already has.

The other way in (rescoring applied *with* the docking) lives in `amdockvs.docking.tasks`: there
the search score is never recorded, only the rescored one, and the row is that one protocol.
Here the docking row exists, so this is a second protocol over the same pose — the source
program plus a rescoring function, a hash of its own — writing its own rows, which point at the
stored pose instead of docking again.
"""
from __future__ import annotations

from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.sinks import table_sink

from amdockvs.binding_sites.repository import active_sites, site_payload
from amdockvs.core.constants import AMDOCKVS_LOCAL_EXECUTORS
from amdockvs.core.jobs import JobSpec
from amdockvs.core.payloads import payload_file
from amdockvs.core.worker_io import worker_file
from amdockvs.docking.protocols import DockingProtocolMetadata, protocol_hash
from amdockvs.docking.rescoring import RESCORING_FUNCTIONS, rescore_stored_poses
from amdockvs.docking.results.jobs import _with_receptor_files
from amdockvs.docking.results.repository import docking_run_id, list_docking_result_rows
from amdockvs.models import BindingSite, DockingResult, DockingRun


class RescoreJobParams(BaseModel):
    """`function` is the rescoring function; the rest picks the stored poses it runs over."""

    function: str = "vinardo"
    result_ids: list[int] = Field(default_factory=list)
    run_id: str = ""
    receptor_id: int | None = Field(default=None, ge=1)
    score_lte: float | None = None
    pose_rank: int | None = Field(default=1, ge=1)
    chunk_size: int = Field(default=32, ge=1)
    vina_command: str = ""


def _boxes(project_db, rows: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    """The box of each (receptor, site) the rows name: the site's own, or the receptor's first
    active site when the row names none (a blind run, an explicit box). `--score_only` needs a
    box — without one the program reads an uninitialized grid."""
    found: dict[tuple[int, int], dict[str, Any]] = {}
    with project_db.get_session() as session:
        for key in {(int(row["receptor_molecule_id"]), int(row.get("binding_site_id") or 0)) for row in rows}:
            site = (session.get(BindingSite, key[1]) if key[1]
                    else next(iter(active_sites(session, key[0])), None))
            found[key] = site_payload(site) or {}
    return found


def _rescoring_run(project_db, source_run_id: int, *, function: str, run_id: str) -> int:
    """The run the new rows point to: the source run's program and config with the rescoring
    function appended, which makes a protocol hash of its own."""
    with project_db.get_session() as session:
        source = session.get(DockingRun, int(source_run_id))
        protocol = DockingProtocolMetadata.from_mapping(dict(getattr(source, "protocol", None) or {}))
        engine = str(getattr(source, "engine", "") or protocol.program or "vina")
        kind = str(getattr(source, "run_kind", "") or "screening")
    rescoring = [*protocol.rescoring, {"program": str(function)}]
    payload = protocol.as_metrics_payload()
    payload["rescoring"] = rescoring
    payload["hash"] = protocol_hash(program=protocol.program or engine, config=protocol.config,
                                    rescoring=rescoring)
    payload["label"] = f"{protocol.label or protocol.program or engine} + {function}"
    return docking_run_id(project_db, run_id=run_id, engine=engine, run_kind=kind, protocol=payload)


class RescoreJobSpec(JobSpec):
    name = "amdock_rescore_job"
    task_name = "amdock_rescore_stored_poses"
    description = "Rescore stored docking poses with another scoring function."
    params_model = RescoreJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = table_sink(model=DockingResult, write_mode="bulk")
    output_flush_every = 10
    store_results = False
    required = ()
    produces = ()

    run_chunk = staticmethod(rescore_stored_poses)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        project_db = dict(config or {}).get("project_db")
        if project_db is None:
            raise ValueError("RescoreJobSpec requires project_db in config.")
        parsed = RescoreJobParams(**params)
        function = str(parsed.function or "").strip().lower()
        if function not in RESCORING_FUNCTIONS:
            raise ValueError(f"Unknown rescoring function '{parsed.function}'. "
                             f"Known: {sorted(RESCORING_FUNCTIONS)}.")
        rows = _with_receptor_files(project_db, list_docking_result_rows(
            project_db,
            result_ids=parsed.result_ids,
            run_id=parsed.run_id,
            receptor_id=parsed.receptor_id,
            score_lte=parsed.score_lte,
            pose_rank=parsed.pose_rank,
        ))
        if not rows:
            return
        # ponytail: the rows of one source run per job; a selection spanning two runs rescores
        # under the first one's protocol. Split the submit if that ever matters.
        run = _rescoring_run(project_db, int(rows[0]["docking_run_id"]), function=function,
                             run_id=f"rescore-{function}-{parsed.run_id or rows[0]['docking_run_id']}")
        boxes = _boxes(project_db, rows)
        chunk_size = max(1, int(parsed.chunk_size))
        for start in range(0, len(rows), chunk_size):
            chunk = []
            for row in rows[start:start + chunk_size]:
                box = boxes.get((int(row["receptor_molecule_id"]), int(row.get("binding_site_id") or 0))) or {}
                if not box.get("center"):
                    continue  # no box, no score
                row["pose_path"] = worker_file(payload_file(project_db, row["pose_payload_id"]))
                row["box_center"], row["box_size"] = box["center"], box["size"]
                chunk.append(row)
            if chunk:
                yield {"rows": chunk, "function": function, "docking_run_id": run,
                       "config": {"vina_command": parsed.vina_command or None}}


rescore_job = RescoreJobSpec.to_job_definition()


__all__ = ["RescoreJobParams", "RescoreJobSpec", "rescore_job"]
