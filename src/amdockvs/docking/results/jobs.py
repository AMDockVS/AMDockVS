"""Jobs that run over results instead of producing them.

Contacts (`ms_contactmap`) and their 2D diagrams: same shape as a docking job — chunk of
result rows in, rows out — but the input already exists in the database. Split from
`docking.jobs` because nothing here calls an engine.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field

from ms_flow.sinks import table_sink
from ms_flow.tasking import JobSpec
from sqlmodel import delete

from amdockvs.core.worker_io import project_root_from_output_dir, worker_file, worker_output_dir
from amdockvs.core.constants import AMDOCKVS_LOCAL_EXECUTORS
from amdockvs.docking.repository import absolutize_pose_paths, resolve_docking_output_dir
from amdockvs.docking.results.interactions import collect_interaction_rows
from amdockvs.docking.results.repository import list_docking_result_rows
from amdockvs.models import InteractionsResult


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
    return absolutize_pose_paths(rows, project_db)


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
        chunk_rows = rows[start:start + chunk_size]
        for row in chunk_rows:
            row["pose_path"] = worker_file(row.get("pose_path"))
            metrics = dict(row.get("metrics") or {})
            metrics["receptor_path"] = worker_file(metrics.get("receptor_path"), cache=True)
            row["metrics"] = metrics
        yield {
            "rows": chunk_rows,
            "method": params.method,
            "output_dir": worker_output_dir(output_dir),
        }


def _relative_to_project(path: Path, project_root: Path | None) -> str:
    if project_root is not None:
        try:
            return str(path.resolve().relative_to(project_root.resolve()))
        except Exception:
            pass
    return str(path)


def _write_interaction_report(
    *,
    output_dir: Path,
    project_root: Path | None,
    result_id: int,
    row: Mapping[str, Any],
    method: str,
    interactions: list[dict[str, Any]],
) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"result_{int(result_id):09d}.interactions.json"
    payload = {
        "schema": "amdockvs.interactions.v1",
        "generated_at": datetime.now().isoformat(),
        "method": method,
        "docking_result_id": int(result_id),
        "receptor_molecule_id": int(row.get("receptor_molecule_id") or 0),
        "ligand_molecule_id": int(row.get("ligand_molecule_id") or 0),
        "engine": str(row.get("engine") or ""),
        "pose_rank": int(row.get("pose_rank") or 1),
        "score": row.get("score"),
        "pose_path": str(row.get("pose_path") or ""),
        "receptor_path": str((row.get("metrics") or {}).get("receptor_path") or ""),
        "interaction_count": len(interactions),
        "interactions": interactions,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    return _relative_to_project(path, project_root)


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

    @staticmethod
    def run_chunk(payload: dict):
        rows_out: list[dict[str, Any]] = []
        method = str(payload.get("method") or "ms_contactmap")
        output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
        project_root = project_root_from_output_dir(output_dir)
        for row in list(payload.get("rows") or []):
            result_id = int(row.get("id") or 0)
            if result_id <= 0:
                continue
            metrics = dict(row.get("metrics") or {})
            pose_path = str(row.get("pose_path") or "")
            receptor_path = str(metrics.get("receptor_path") or "")
            interactions = collect_interaction_rows(
                pose_path=pose_path,
                receptor_path=receptor_path,
                pose_rank=int(row.get("pose_rank") or 1),
            )
            json_path = _write_interaction_report(
                output_dir=output_dir,
                project_root=project_root,
                result_id=result_id,
                row=row,
                method=method,
                interactions=interactions,
            )
            if interactions:
                for interaction in interactions:
                    geometry = dict(interaction.get("geometry") or {})
                    geometry["json_path"] = json_path
                    interaction["geometry"] = geometry
                rows_out.extend(InteractionsResult.build_rows(result_id, interactions))
            else:
                rows_out.extend(
                    InteractionsResult.build_rows(
                        result_id,
                        [
                            {
                                "interaction_type": "none",
                                "residue": "",
                                "residue_index": 0,
                                "distance": None,
                                "geometry": {
                                    "json_path": json_path,
                                    "method": method,
                                    "interaction_count": 0,
                                },
                            }
                        ],
                    )
                )
        return rows_out

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
    return absolutize_pose_paths(
        list_docking_result_rows(
            project_db,
            result_ids=params.result_ids,
            run_id=params.run_id,
            receptor_id=params.receptor_id,
            score_lte=params.score_lte,
            pose_rank=params.pose_rank,
        ),
        project_db,
    )


class DiagramJobSpec(JobSpec):
    name = "amdock_diagram_job"
    task_name = "amdock_render_interaction_diagrams"
    description = "Render 2D protein-ligand interaction diagrams for docking poses."
    params_model = DiagramJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = None  # writes PNG/SVG next to each pose; no DB rows
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def run_chunk(payload: dict):
        from amdockvs.docking.results.diagram import render_diagrams_for_result_rows

        render_diagrams_for_result_rows(
            list(payload.get("rows") or []),
            fmt=str(payload.get("fmt") or "png"),
            replace_existing=bool(payload.get("replace_existing")),
            output_dir=str(payload.get("output_dir") or "") or None,
        )
        return []

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("DiagramJobSpec requires project_db in config.")
        parsed = DiagramJobParams(**params)
        rows = _diagram_result_rows(project_db, parsed)
        output_dir = resolve_docking_output_dir({}, config_map)
        chunk_size = max(1, int(parsed.chunk_size))
        for start in range(0, len(rows), chunk_size) or [0]:
            chunk_rows = rows[start:start + chunk_size]
            for row in chunk_rows:
                row["pose_path"] = worker_file(row.get("pose_path"))
                metrics = dict(row.get("metrics") or {})
                metrics["receptor_path"] = worker_file(metrics.get("receptor_path"), cache=True)
                row["metrics"] = metrics
            yield {
                "rows": chunk_rows,
                "fmt": parsed.fmt,
                "replace_existing": parsed.replace_existing,
                "output_dir": worker_output_dir(output_dir),
            }


diagram_job = DiagramJobSpec.to_job_definition()
