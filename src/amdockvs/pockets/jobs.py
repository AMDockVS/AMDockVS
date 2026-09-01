"""MolSuite job definition for P2Rank pocket prediction."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field, field_validator

from ms_flow.query import QuerySpec, db_pages
from ms_flow.sinks import table_sink
from ms_flow.tasking import job, task

from amdockvs.constants import (
    AMDOCKVS_PROCESS_EXECUTORS,
    RESOURCE_POCKET_PREDICTIONS,
    TABLE_MOLECULES,
)
from amdockvs.api_common import worker_file, worker_output_dir
from amdockvs.models import BindingSite
from amdockvs.molecule_paths import preferred_molecule_path, set_default_project_root
from amdockvs.pockets.p2rank import P2RANK_VERSION, run_p2rank_prediction


class P2RankPredictionParams(BaseModel):
    receptor_ids: list[int] = Field(default_factory=list)
    profile: str = Field(default="default")
    profiles: dict[int, str] = Field(default_factory=dict)
    recalculate_receptor_ids: list[int] = Field(default_factory=list)
    threads: int = Field(default=1, ge=1, le=128)
    run_id: str
    output_dir: str = ""
    p2rank_command: str
    java_command: str
    version: str = P2RANK_VERSION

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        normalized = str(value or "default").strip().lower()
        if normalized not in {"default", "alphafold"}:
            raise ValueError("profile must be 'default' or 'alphafold'.")
        return normalized

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or re.fullmatch(r"[A-Za-z0-9_.-]+", normalized) is None
        ):
            raise ValueError("run_id must contain only letters, numbers, '.', '_' or '-'.")
        return normalized

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: dict[int, str]) -> dict[int, str]:
        normalized: dict[int, str] = {}
        for raw_id, raw_profile in dict(value or {}).items():
            receptor_id = int(raw_id)
            profile = str(raw_profile or "default").strip().lower()
            if receptor_id <= 0:
                raise ValueError("profile receptor ids must be positive.")
            if profile not in {"default", "alphafold"}:
                raise ValueError("profiles values must be 'default' or 'alphafold'.")
            normalized[receptor_id] = profile
        return normalized

    @field_validator("recalculate_receptor_ids")
    @classmethod
    def validate_recalculate_receptor_ids(cls, value: list[int]) -> list[int]:
        return sorted({int(raw_id) for raw_id in value if int(raw_id) > 0})


@task(
    name="amdock_p2rank_predict_receptor",
    description="Predict receptor pockets with P2Rank and stage their native outputs.",
    executor="compute",
    supported_executors=AMDOCKVS_PROCESS_EXECUTORS,
)
def p2rank_prediction_task(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if bool(payload.get("reuse_only")):
        return []
    return run_p2rank_prediction(payload)


def _output_root(params: P2RankPredictionParams, config: dict[str, Any]) -> Path:
    if str(params.output_dir or "").strip():
        root = Path(params.output_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    resources = dict(config.get("project_resources") or {})
    resource = dict(resources.get(RESOURCE_POCKET_PREDICTIONS) or {})
    path = str(resource.get("path") or "").strip()
    if not path:
        raise ValueError(
            "P2Rank requires output_dir or the project resource 'pocket_predictions'."
        )
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def scope_spec(params: P2RankPredictionParams) -> QuerySpec:
    """The scope of a pocket prediction, fully declared: the active receptors.

    `params.receptor_ids` is an explicit user list and is bounded by construction, so `id__in`
    is correct here — it is not a materialisation the size of the library.
    """
    filters: dict[str, Any] = {"is_receptor": True, "excluded": False}
    if params.receptor_ids:
        filters["id__in"] = [int(value) for value in params.receptor_ids]
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "name", "stored_path", "current_path", "current_model_index", "extra_data"),
        filters=filters,
        order=("id",),
    )


def _profile_for(params: P2RankPredictionParams, receptor_id: int) -> str:
    return str(params.profiles.get(int(receptor_id), params.profile) or "default")


def _prediction_output_dir(
    output_root: Path,
    *,
    run_id: str,
    receptor_id: int,
) -> Path:
    return output_root / str(run_id) / f"receptor_{int(receptor_id)}"


def p2rank_prediction_finalize(
    _payload: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """The only thing left to do by hand: drop the files of a run that failed.

    The rows are written by the sink. Prediction is additive — a new run sits next to the old
    ones and it is the user who deletes the ones they no longer want
    (`pockets.delete_binding_sites`), so no row and no active receptor site is touched here.
    """
    raw_params = context.get("job_params")
    if not isinstance(raw_params, dict):
        raise ValueError("P2Rank finalization requires job_params in the MolSuite context.")
    params = P2RankPredictionParams.model_validate(raw_params)
    if str(context.get("terminal_status") or "").strip().lower() == "completed":
        return {"predicted_receptors": len(params.recalculate_receptor_ids), "status": "added"}

    output_root = _output_root(
        params,
        {"project_resources": dict(context.get("project_resources") or {})},
    )
    failed_run = output_root / params.run_id
    if failed_run.is_dir():
        shutil.rmtree(failed_run)
    return {"predicted_receptors": 0, "status": "skipped_failed_job"}


@job(
    task=p2rank_prediction_task,
    name="amdock_pocket_prediction_job",
    params_model=P2RankPredictionParams,
    executor="compute",
    supported_executors=AMDOCKVS_PROCESS_EXECUTORS,
    output_spec=table_sink(model=BindingSite, write_mode="bulk"),
    finalize=p2rank_prediction_finalize,
    store_results=False,
)
def p2rank_prediction_job(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
    parsed = P2RankPredictionParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("p2rank_prediction_job requires project_db in config.")
    db_path = getattr(project_db, "db_path", None)
    if db_path is None:
        raise ValueError("p2rank_prediction_job requires project_db.db_path.")
    project_root = Path(db_path).expanduser().resolve().parent
    set_default_project_root(project_root)
    output_root = _output_root(parsed, config_map)

    recalculate_ids = set(parsed.recalculate_receptor_ids)
    emitted = False
    in_scope = False
    for receptor in db_pages(project_db, scope_spec(parsed), page_size=32):
        in_scope = True
        receptor_id = int(receptor.get("id") or 0)
        if receptor_id not in recalculate_ids:
            continue
        receptor_path = preferred_molecule_path(receptor)
        if receptor_path is None or not receptor_path.is_file():
            raise ValueError(f"Receptor {receptor_id} has no readable structure file.")
        emitted = True
        yield {
            "receptor_id": receptor_id,
            "receptor_name": str(receptor.get("name") or f"receptor_{receptor_id}"),
            "receptor_path": worker_file(receptor_path, cache=True),
            "profile": _profile_for(parsed, receptor_id),
            "threads": int(parsed.threads),
            "run_id": parsed.run_id,
            "version": parsed.version,
            "p2rank_command": parsed.p2rank_command,
            "java_command": parsed.java_command,
            "output_dir": worker_output_dir(
                _prediction_output_dir(
                    output_root,
                    run_id=parsed.run_id,
                    receptor_id=receptor_id,
                )
            ),
        }
    if not in_scope:
        raise ValueError("P2Rank prediction requires at least one active receptor.")
    if not emitted:
        yield {"reuse_only": True}


__all__ = [
    "P2RankPredictionParams",
    "p2rank_prediction_finalize",
    "p2rank_prediction_job",
    "p2rank_prediction_task",
]
