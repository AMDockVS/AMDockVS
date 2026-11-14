"""MolSuite job definition for P2Rank pocket prediction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field, field_validator

from ms_flow.query import QuerySpec, db_pages
from ms_flow.sinks import table_sink
from amdockvs.core.jobs import job
from amdockvs.binding_sites.tasks import p2rank_prediction_task

from amdockvs.core.constants import (
    AMDOCKVS_PROCESS_EXECUTORS,
    RESOURCE_POCKET_PREDICTIONS,
    TABLE_MOLECULE_MODELS,
    TABLE_MOLECULES,
)
from amdockvs.core.configuration import DEFAULT_P2RANK_PROFILE
from amdockvs.core.vocab import ModelSource
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.models import BindingSite
from amdockvs.core.paths import set_default_project_root
from amdockvs.core.payloads import molecule_payload_id, payload_file
from amdockvs.binding_sites.p2rank import P2RANK_PROFILES, P2RANK_VERSION


class P2RankPredictionParams(BaseModel):
    receptor_ids: list[int] = Field(default_factory=list)
    profile: str = Field(default=DEFAULT_P2RANK_PROFILE)
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
        normalized = str(value or DEFAULT_P2RANK_PROFILE).strip().lower()
        if normalized not in P2RANK_PROFILES:
            raise ValueError(f"profile must be one of {', '.join(P2RANK_PROFILES)}.")
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
            profile = str(raw_profile or DEFAULT_P2RANK_PROFILE).strip().lower()
            if receptor_id <= 0:
                raise ValueError("profile receptor ids must be positive.")
            if profile not in P2RANK_PROFILES:
                raise ValueError(f"profiles values must be one of {', '.join(P2RANK_PROFILES)}.")
            normalized[receptor_id] = profile
        return normalized

    @field_validator("recalculate_receptor_ids")
    @classmethod
    def validate_recalculate_receptor_ids(cls, value: list[int]) -> list[int]:
        return sorted({int(raw_id) for raw_id in value if int(raw_id) > 0})


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
    filters: dict[str, Any] = {"is_receptor": True, "excluded": False, "has_3d": True}
    if params.receptor_ids:
        filters["id__in"] = [int(value) for value in params.receptor_ids]
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "name", "stored_payload_id", "current_payload_id", "extra_data"),
        filters=filters,
        order=("id",),
    )


MISSING_STRUCTURE_HINT = (
    "Generate one in Build > Proteins > 3D generation (ESMFold), or import a PDB/mmCIF file."
)


def missing_structure_message(names) -> str:
    return f"P2Rank cannot run: these receptors have no 3D structure: {', '.join(names)}. " + MISSING_STRUCTURE_HINT


def _raise_unreadable_scope(project_db, params: P2RankPredictionParams) -> None:
    """Nothing in scope: say which requested receptors exist but have no structure to read."""
    spec = QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "name"),
        filters={"is_receptor": True, "excluded": False, "has_3d": False,
                 **({"id__in": [int(v) for v in params.receptor_ids]} if params.receptor_ids else {})},
        order=("id",),
    )
    names = [str(row.get("name") or f"receptor_{row['id']}") for row in db_pages(project_db, spec)]
    if names:
        raise ValueError(missing_structure_message(names))
    raise ValueError("P2Rank prediction requires at least one active receptor.")


def _profile_for(params: P2RankPredictionParams, receptor_id: int) -> str:
    return str(params.profiles.get(int(receptor_id), params.profile) or DEFAULT_P2RANK_PROFILE)


def _predicted_molecule_ids(project_db) -> set[int]:
    """Receptors with a predicted model among theirs, read once for the project.

    By molecule, not by payload: fixing or protonating a prediction makes a new model whose
    B-factors are still pLDDT (reduce keeps them) or zero (OpenMM). Only read when a run asks
    for "auto", the only case whose outcome depends on it.
    """
    spec = QuerySpec(
        table=TABLE_MOLECULE_MODELS,
        fields=("molecule_id",),
        filters={"source__in": sorted(ModelSource.PREDICTED)},
    )
    return {int(row["molecule_id"]) for row in db_pages(project_db, spec, page_size=512)}


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
    """Leave prediction outputs alongside the rows already committed by the sink.

    Results are committed per receptor. A canceled or failed batch can therefore contain
    successful predictions whose points files the UI still needs. Keep the run directory,
    including partial files, rather than deleting outputs referenced by persisted sites.
    Prediction is additive; no row or active receptor site is changed here.
    """
    raw_params = context.get("job_params")
    if not isinstance(raw_params, dict):
        raise ValueError("P2Rank finalization requires job_params in the MolSuite context.")
    params = P2RankPredictionParams.model_validate(raw_params)
    if str(context.get("terminal_status") or "").strip().lower() == "completed":
        return {"predicted_receptors": len(params.recalculate_receptor_ids), "status": "added"}

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
    # The executor decides each receptor's profile from the structure itself; this is the half
    # of that decision only the project database can answer.
    resolves = any(_profile_for(parsed, receptor_id) == "auto" for receptor_id in recalculate_ids)
    predicted_ids = _predicted_molecule_ids(project_db) if resolves else set()
    emitted = False
    in_scope = False
    for receptor in db_pages(project_db, scope_spec(parsed), page_size=32):
        in_scope = True
        receptor_id = int(receptor.get("id") or 0)
        if receptor_id not in recalculate_ids:
            continue
        payload_id = molecule_payload_id(receptor)
        receptor_path = payload_file(project_db, payload_id)  # P2Rank only reads paths
        if receptor_path is None:
            raise ValueError(f"Receptor {receptor_id} has no structure.")
        emitted = True
        yield {
            "receptor_id": receptor_id,
            "receptor_name": str(receptor.get("name") or f"receptor_{receptor_id}"),
            "receptor_path": worker_file(receptor_path, cache=True),
            "profile": _profile_for(parsed, receptor_id),
            "predicted": receptor_id in predicted_ids,
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
        _raise_unreadable_scope(project_db, parsed)
    if not emitted:
        yield {"reuse_only": True}


__all__ = [
    "MISSING_STRUCTURE_HINT",
    "missing_structure_message",
    "P2RankPredictionParams",
    "p2rank_prediction_finalize",
    "p2rank_prediction_job",
    "p2rank_prediction_task",
]
