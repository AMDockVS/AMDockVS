"""Jobs that run over results instead of producing them.

Contacts (`ms_contactmap`) and their 2D diagrams: same shape as a docking job — chunk of
result rows in, rows out — but the input already exists in the database. Split from
`docking.jobs` because nothing here calls an engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.sinks import table_sink
from amdockvs.core.jobs import JobSpec
from amdockvs.docking.results.tasks import diagrams_of_poses, interactions_of_poses
from sqlmodel import delete

from ms_flow.payloads import with_payloads

from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.core.constants import AMDOCKVS_LOCAL_EXECUTORS
from amdockvs.core.payloads import PAYLOAD_CACHE, molecule_payload_id, payload_file
from amdockvs.docking.repository import project_db_path, resolve_docking_output_dir
from amdockvs.docking.results.repository import list_docking_result_rows
from amdockvs.models import InteractionsResult, MoleculeRecord


class InteractionJobParams(BaseModel):
    output_dir: str | None = None
    result_ids: list[int] = Field(default_factory=list)
    run_id: str = ""
    receptor_id: int | None = Field(default=None, ge=1)
    score_lte: float | None = None
    pose_rank: int | None = Field(default=1, ge=1)
    method: str = "ms_contactmap"
    chunk_size: int = Field(default=128, ge=1)
    replace_existing: bool = True


class DiagramJobParams(BaseModel):
    result_ids: list[int] = Field(default_factory=list)
    run_id: str = ""
    receptor_id: int | None = Field(default=None, ge=1)
    score_lte: float | None = None
    pose_rank: int | None = Field(default=1, ge=1)
    fmt: str = "png"
    chunk_size: int = Field(default=64, ge=1)
    replace_existing: bool = False


def _with_receptor_files(project_db, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each row with `receptor_path`, the structure its pose was docked into (else the receptor's
    own), written once to the project's payload cache: every pose of a receptor shares it."""
    missing = {int(row["receptor_molecule_id"]) for row in rows
               if not (row.get("metrics") or {}).get("receptor_payload_id")}
    with project_db.get_session() as session:
        own = {rid: molecule_payload_id(session.get(MoleculeRecord, rid)) for rid in missing}
    files: dict[int, Any] = {}
    for row in rows:
        payload_id = (row.get("metrics") or {}).get("receptor_payload_id") or own.get(int(row["receptor_molecule_id"]))
        if payload_id and payload_id not in files:
            files[payload_id] = worker_file(payload_file(project_db, payload_id), cache=True)
        row["receptor_path"] = files.get(payload_id, "")
    return rows


def _interaction_result_rows(project_db, params: InteractionJobParams) -> list[dict[str, Any]]:
    rows = list_docking_result_rows(
        project_db,
        result_ids=params.result_ids,
        run_id=params.run_id,
        receptor_id=params.receptor_id,
        score_lte=params.score_lte,
        pose_rank=params.pose_rank,
    )
    result_ids = [int(row.get("id") or 0) for row in rows if int(row.get("id") or 0) > 0]
    if params.replace_existing and result_ids:
        with project_db.get_session() as session:
            session.exec(delete(InteractionsResult).where(InteractionsResult.docking_result_id.in_(result_ids)))
            session.commit()
    return _with_receptor_files(project_db, rows)


def _iter_interaction_chunks(
    *,
    project_db,
    params: InteractionJobParams,
    output_dir: Path,
) -> Iterator[dict[str, Any]]:
    rows = _interaction_result_rows(project_db, params)
    chunk_size = max(1, int(params.chunk_size))
    if not rows:
        yield {"rows": [], "method": params.method, "output_dir": worker_output_dir(output_dir)}
        return
    for start in range(0, len(rows), chunk_size):
        yield {
            "rows": list(with_payloads(project_db, rows[start:start + chunk_size], {"pose_payload_id": "pose"})),
            "method": params.method,
            "output_dir": worker_output_dir(output_dir),
        }


class InteractionJobSpec(JobSpec):
    name = "amdock_interactions_job"
    task_name = "amdock_compute_interactions"
    description = "Compute protein-ligand interactions for docking poses."
    params_model = InteractionJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = table_sink(model=InteractionsResult, write_mode="bulk")
    output_flush_every = 25
    store_results = False
    required = ()
    produces = ()

    run_chunk = staticmethod(interactions_of_poses)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("InteractionJobSpec requires project_db in config.")
        parsed = InteractionJobParams(**params)
        output_dir = resolve_docking_output_dir({"output_dir": parsed.output_dir}, config_map) / "interactions"
        yield from _iter_interaction_chunks(project_db=project_db, params=parsed, output_dir=output_dir)


interactions_job = InteractionJobSpec.to_job_definition()


def _diagram_result_rows(project_db, params: DiagramJobParams) -> list[dict[str, Any]]:
    return _with_receptor_files(
        project_db,
        list_docking_result_rows(
            project_db,
            result_ids=params.result_ids,
            run_id=params.run_id,
            receptor_id=params.receptor_id,
            score_lte=params.score_lte,
            pose_rank=params.pose_rank,
        ),
    )


class DiagramJobSpec(JobSpec):
    name = "amdock_diagram_job"
    task_name = "amdock_render_interaction_diagrams"
    description = "Render 2D protein-ligand interaction diagrams for docking poses."
    params_model = DiagramJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = None  # writes PNG/SVG next to each pose in the payload cache; no DB rows
    store_results = False
    required = ()
    produces = ()

    run_chunk = staticmethod(diagrams_of_poses)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("DiagramJobSpec requires project_db in config.")
        parsed = DiagramJobParams(**params)
        rows = _diagram_result_rows(project_db, parsed)
        # A diagram sits beside its pose in the payload cache, named after the pose's payload id
        # (diagram_path_for): the viewer finds it from the pose alone.
        output_dir = project_db_path(project_db).parent / PAYLOAD_CACHE
        chunk_size = max(1, int(parsed.chunk_size))
        for start in range(0, len(rows), chunk_size) or [0]:
            chunk_rows = rows[start:start + chunk_size]
            for row in chunk_rows:
                row["pose_path"] = worker_file(payload_file(project_db, row["pose_payload_id"]))
            yield {
                "rows": chunk_rows,
                "fmt": parsed.fmt,
                "replace_existing": parsed.replace_existing,
                "output_dir": worker_output_dir(output_dir),
            }


diagram_job = DiagramJobSpec.to_job_definition()
