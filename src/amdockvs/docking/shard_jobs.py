"""Docking a sharded library: one chunk is one prepared shard against one receptor.

The `vs` twin (`docking/jobs.py`) pairs database rows and writes one result row per pose.
Neither half survives a screening library: there are no ligand rows to pair, and writing every
pose back as DB rows is exactly what the hit gate exists to prevent. What travels out is
`(shard, receptor)`; every result is packed into one result shard, while only the handful of
ligands that beat the threshold comes back to become molecules — via `ShardStore.materialize`,
in the parent.

The unit is inherited from preparation: the shard *is* the batch, so a chunk carries exactly one.
What bounds memory is therefore the shard size chosen at import
(~1000 records), not a slice. The `.pdbqt` files the engine reads exist for the length of one
shard inside a temporary directory, because a file per ligand is what a screening library
cannot afford to keep.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import select

from ms_flow.tasking import JobSpec
from ms_flow.core.data.shard import Shard

from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.core.configuration import DEFAULT_HIT_SAFETY_CAP
from amdockvs.core.constants import AMDOCKVS_LOCAL_EXECUTORS, DEFAULT_VINA_BACKEND, DEFAULT_VINA_COMMAND
from amdockvs.docking.engines.registry import run_docking_chunk
from amdockvs.docking.engines.programs import chunk_resources
from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.repository import list_entity_rows, resolve_docking_output_dir
from amdockvs.docking.results.repository import count_docking_results
from amdockvs.docking.results.dataset import (
    RESULT_DATASET_FORMAT,
    ResultFragmentWriter,
    completed_result_receipts,
    local_pose_path,
    result_fragment_path,
    result_metadata,
    result_row,
    select_hit_refs,
    select_hits,
)
from amdockvs.docking.preparation.state import docking_input_path_from_row, grid_from_row
from amdockvs.docking.preparation.shards import extract_ligands
from amdockvs.screening.campaign import (
    advance_shard_run,
    advance_target,
    completed_shard_targets,
    finish_run,
    plan_targets,
    stop_run,
)
from amdockvs.screening.materialize import HitGate, PAYLOAD_LIGHT, ingest_hits
from amdockvs.models import DockingResultRecord, ScreeningShardRun, ScreeningTarget
from amdockvs.core.paths import get_default_project_root
from amdockvs.molecules.storage import ShardStore, iter_prepared_shards
from amdockvs.core.vocab import TargetState

# Poses of a campaign live apart from `vs` results: shard record ids and molecule ids both start
# at 0, and the pose file name is built from the ligand id.
SCREENING_SUBDIR = "screening"

# How the cap is spent. `threshold`: the first `cap` ligands that beat the threshold, written as
# they come back — the campaign can stop early. `top_n`: the best `cap` of everything scored,
# written when the run ends — no early stop, and nothing to look at until then.
HIT_MODE_THRESHOLD = "threshold"
HIT_MODE_TOP_N = "top_n"
HIT_MODES = (HIT_MODE_THRESHOLD, HIT_MODE_TOP_N)

# By-threshold has no hit count of its own — "everything under the cutoff" is the criterion.
# This is only the unattended-run ceiling: a campaign that reaches it has a wrong threshold,
# not a lucky library. Settings > shards > hit_cap; this is the packaged default for code
# evaluated before a runtime exists.
HIT_SAFETY_CAP = DEFAULT_HIT_SAFETY_CAP

# The prefilter a ranked run uses: none. Docking scores are negative, so this only drops poses
# that did not bind at all — "best N of the whole run" must not quietly exclude what the user
# never set a cutoff for.
RANKED_PREFILTER_SCORE = 0.0

RESULT_SHARD_FORMAT = RESULT_DATASET_FORMAT


def result_shard_path(output_dir: str | Path, payload: dict, shard: dict) -> Path:
    """Compatibility name for the Parquet result-fragment path."""
    return result_fragment_path(output_dir, payload, shard)


def _local_pose_path(value: object) -> Path | None:
    return local_pose_path(value)


def _embedded_pose(payload: dict) -> tuple[str, str]:
    """Read v2 generic poses and the v1 SDF representation kept for old projects."""
    if "pose_text" in payload:
        suffix = str(payload.get("pose_suffix") or ".pose").lower()
        if not suffix.startswith(".") or not suffix[1:].isalnum():
            suffix = ".pose"
        return str(payload.get("pose_text") or ""), suffix
    return str(payload.get("sdf") or ""), ".sdf"


def _materialize_pose_files(
    rows: list[dict[str, Any]], *, project_root: str | Path | None = None
) -> list[dict[str, Any]]:
    """Restore individual pose files only for rows that the hit gate selected."""
    root = Path(project_root) if project_root is not None else get_default_project_root()
    materialized: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        result_text = str(row.pop("_result_shard", "") or "")
        record_id = int(row.pop("_result_record_id", row.get("source_index") or 0))
        shard_id = int(row.pop("_shard_id", 0) or 0)
        has_embedded = "_pose_text" in row
        pose_text = str(row.pop("_pose_text", "") or "")
        pose_suffix = str(row.pop("_pose_suffix", ".pose") or ".pose")
        result_path = Path(result_text)
        if result_text and not result_path.is_absolute() and root is not None:
            result_path = root / result_path
        if result_text and not has_embedded and not result_path.is_file():
            raise FileNotFoundError(f"Selected pose result shard not found: {result_path}")
        if not has_embedded and result_path.is_file():
            with Shard.open(result_path) as shard:
                payload = json.loads(gzip.decompress(shard[record_id].load()))
                pose_text, pose_suffix = _embedded_pose(payload)
                shard_id = shard_id or int(shard.shard_id)
        if result_text and not pose_text:
            raise ValueError(
                f"Result shard {result_path} record {record_id} has no embedded pose."
            )
        if pose_text and result_text:
            pose_dir = result_path.parent / "selected"
            pose_dir.mkdir(parents=True, exist_ok=True)
            pose_path = pose_dir / (
                f"shard_{shard_id:05d}_ligand_{int(row.get('source_index') or 0):09d}"
                f"{pose_suffix}"
            )
            temporary = pose_path.with_name(f"{pose_path.name}.tmp")
            temporary.write_text(pose_text, encoding="utf-8")
            temporary.replace(pose_path)
            row["pose_path"] = str(pose_path)
            metrics = dict(row.get("metrics") or {})
            metrics["selected_pose_path"] = str(pose_path)
            metrics["selected_pose_pdbqt_path"] = ""
            row["metrics"] = metrics
        materialized.append(row)
    return materialized


def _project_path(project_root: str | Path, value: str) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else Path(project_root) / path


def _materialized_count(project_db, *, run_id: str, protocol_hash: str) -> int:
    with project_db.get_session() as session:
        statement = select(func.count()).select_from(DockingResultRecord).where(
            DockingResultRecord.metrics["run_id"].as_string() == str(run_id),
            DockingResultRecord.metrics["protocol"]["hash"].as_string() == str(protocol_hash),
        )
        return int(session.exec(statement).one())


def recoverable_ranked_runs(project_db, *, project_root: str | Path) -> list[dict[str, Any]]:
    """Terminal ranked campaigns whose completed result shards have not been materialized."""
    with project_db.get_session() as session:
        receipts = session.exec(
            select(ScreeningShardRun)
            .where(ScreeningShardRun.state == TargetState.DONE, ScreeningShardRun.result_path != "")
            .order_by(ScreeningShardRun.updated_at.desc())
        ).all()
        target_states: dict[str, set[str]] = {}
        for target in session.exec(select(ScreeningTarget)).all():
            target_states.setdefault(str(target.run_id), set()).add(str(target.state))

    groups: dict[tuple[str, str], list[ScreeningShardRun]] = {}
    for receipt in receipts:
        groups.setdefault((str(receipt.run_id), str(receipt.protocol_hash or "")), []).append(receipt)

    candidates: list[dict[str, Any]] = []
    active = {TargetState.SCHEDULED, TargetState.RUNNING}
    for (run_id, protocol_hash), rows in groups.items():
        if target_states.get(run_id, set()) & active:
            continue
        if _materialized_count(project_db, run_id=run_id, protocol_hash=protocol_hash):
            continue
        metadata: dict[str, Any] = {}
        paths = [_project_path(project_root, row.result_path) for row in rows]
        for path in paths:
            if not path.is_file():
                continue
            metadata = result_metadata(path)
            break
        if not metadata:
            continue
        selection = dict(metadata.get("selection") or {})
        mode = str(selection.get("mode") or "unknown")
        if mode not in {HIT_MODE_TOP_N, "unknown"}:
            continue
        protocol = dict(metadata.get("protocol") or {})
        candidates.append({
            "run_id": run_id,
            "protocol_hash": protocol_hash,
            "protocol_label": str(protocol.get("label") or protocol.get("program") or rows[0].engine),
            "status": ", ".join(sorted(target_states.get(run_id, {"terminal"}))),
            "top_n": max(0, int(selection.get("cap") or 0)),
            "threshold": float(selection.get("threshold", RANKED_PREFILTER_SCORE)),
            "shards": sum(path.is_file() for path in paths),
            "scored": sum(max(0, int(row.ligands_scored or 0)) for row in rows),
            "updated_at": max(row.updated_at for row in rows),
        })
    return sorted(candidates, key=lambda row: row["updated_at"], reverse=True)


def recover_ranked_hits(
    project_db,
    *,
    project_root: str | Path,
    run_id: str,
    protocol_hash: str,
    top_n: int,
    threshold: float = RANKED_PREFILTER_SCORE,
) -> dict[str, int]:
    """Rebuild a partial top-N from completed result shards, without rerunning docking."""
    cap = int(top_n)
    if cap <= 0:
        raise ValueError("top_n must be greater than zero.")
    if _materialized_count(project_db, run_id=run_id, protocol_hash=protocol_hash):
        raise ValueError("This run/protocol already has materialized docking results.")

    receipts = completed_result_receipts(
        project_db, run_id=run_id, protocol_hash=protocol_hash
    )
    readable_shards = sum(
        _project_path(project_root, receipt.result_path).is_file() for receipt in receipts
    )
    scanned = sum(max(0, int(receipt.ligands_scored or 0)) for receipt in receipts)
    receptor_ids = sorted({int(receipt.receptor_molecule_id) for receipt in receipts})
    selected = _materialize_pose_files([
        hit
        for receptor_id in receptor_ids
        for hit in select_hits(
            project_db,
            project_root=project_root,
            run_id=run_id,
            protocol_hash=protocol_hash,
            filters={"score__lte": float(threshold), "receptor_id": receptor_id},
            top_n=cap,
        )
    ], project_root=project_root)

    materialized = ingest_hits(
        project_db,
        selected,
        gate=HitGate(cap=max(1, len(selected))),
        payload=PAYLOAD_LIGHT,
        project_root=project_root,
        reuse_existing=True,
    ) if selected else []
    return {
        "shards": readable_shards,
        "scored": scanned,
        "selected": len(materialized),
    }


class DockShardsJobParams(BaseModel):
    """`DockingJobParams` minus everything that assumes rows, plus a mandatory gate.

    The shard is the batch cut at import and one shard/receptor pair is one task. Parallelism
    is therefore bought at import; Ray distributes those tasks over the resources reserved by MF.
    """

    output_dir: str | None = None
    engine: str = "vina"
    preparation_engine: str = "ad4"
    receptor_set_id: int | None = Field(default=None, ge=1)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)
    exhaustiveness: int = 8
    num_modes: int = 9
    box_center: tuple[float, float, float] | None = None
    box_size: tuple[float, float, float] | None = None
    scoring_function: str = "vina"
    vina_backend: str = DEFAULT_VINA_BACKEND
    vina_command: str = DEFAULT_VINA_COMMAND
    vina_cpu: int = Field(default=1, ge=0)
    seed: int = 0
    spacing: float = Field(default=0.375, gt=0.0)
    energy_range: float = Field(default=3.0, ge=0.0)
    min_rmsd: float = Field(default=1.0, ge=0.0)
    run_id: str = ""
    protocol_metadata: DockingProtocolMetadata = Field(default_factory=DockingProtocolMetadata)
    engine_config: dict[str, Any] = Field(default_factory=dict)
    # Neither is optional here: without a threshold every ligand comes back as a hit, and
    # without a cap a generous receptor materializes the library the mode exists to keep out.
    hit_threshold: float
    hit_cap: int = Field(ge=1)
    hit_mode: str = HIT_MODE_THRESHOLD
    # Set only for the dependent off-target stage. Once the reference job completes, its
    # durable ranking supplies the record ids; only those records are extracted from each shard.
    selected_from_receptor_id: int | None = Field(default=None, ge=1)
    selected_top_n: int | None = Field(default=None, ge=1)
    skip_existing: bool = True
    check_required: bool = True


def _pose_files(row: dict) -> list[str]:
    metrics = dict(row.get("metrics") or {})
    return [
        str(row.get("pose_path") or ""),
        str(metrics.get("selected_pose_path") or ""),
        str(metrics.get("selected_pose_pdbqt_path") or ""),
    ]


def _discard_poses(rows: list[dict]) -> None:
    """Delete the poses of everything that missed the threshold.

    The non-hits are the whole library minus a handful. Their files are written before the
    score is known, so this is where the campaign stops filling the disk.
    """
    for row in rows:
        for value in set(_pose_files(row)):
            path = _local_pose_path(value)
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def _hit_row(row: dict, *, smiles: str, source: str, shard_index: int) -> dict[str, Any]:
    """A docking result row turned into what `ingest_hits` promotes: a molecule and its pose."""
    ligand_id = int(row.get("ligand_molecule_id") or 0)
    metrics = {**dict(row.get("metrics") or {}), "shard_source": source, "shard_index": shard_index,
               "shard_record_id": ligand_id}
    return {
        "name": f"{Path(source).stem or 'library'}:{ligand_id}",
        "source": source,
        # The shard record id *is* the identity of a library molecule: the shard's
        # `source_indices` metadata is what maps it back to a line in the original file.
        "source_index": ligand_id,
        "smiles": smiles,
        "engine": str(row.get("engine") or ""),
        "score": row.get("score"),
        "score_type": str(row.get("score_type") or ""),
        "pose_path": str(row.get("pose_path") or ""),
        "pose_rank": int(row.get("pose_rank") or 1),
        "receptor_molecule_id": int(row.get("receptor_molecule_id") or 0),
        "metrics": metrics,
    }


class ShardHitWriter:
    """Parent-side sink: promote hits and write campaign-scoped shard receipts.

    Not an `output_spec` because both halves need the parent — the gate counts across chunks
    (§4) and the shard's state row is not a result row.
    """

    def __init__(
        self,
        *,
        project_db: Any,
        hit_cap: int,
        hit_threshold: float,
        payload: str = PAYLOAD_LIGHT,
        hit_mode: str = HIT_MODE_THRESHOLD,
        run_id: str = "",
        engine: str = "",
        protocol_hash: str = "",
        selected_from_receptor_id: int | None = None,
    ) -> None:
        self.project_db = project_db
        self.run_id = str(run_id)
        self.engine = str(engine)
        self.protocol_hash = str(protocol_hash or "")
        self.hit_mode = str(hit_mode)
        self.hit_cap = int(hit_cap)
        self.hit_threshold = float(hit_threshold)
        self.selected_from_receptor_id = (
            int(selected_from_receptor_id) if selected_from_receptor_id else None
        )
        self.terminal_status = "completed"
        self.store = ShardStore(
            project_db,
            gate=HitGate(cap=self.hit_cap, threshold=self.hit_threshold),
            payload=payload,
        )

    def _materialize_selected(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        selected = _materialize_pose_files(rows)
        return ingest_hits(
            self.project_db,
            selected,
            gate=HitGate(cap=len(selected)),
            payload=self.store.payload,
            project_root=get_default_project_root(),
            reuse_existing=True,
        )

    def handle(self, chunk_id: str, result: Any) -> None:
        for envelope in list(result or []):
            source = str(envelope.get("source") or "")
            shard_index = int(envelope.get("shard_index") or 0)
            shard_id = int(envelope.get("shard_id") or 0)
            if not source:
                continue
            hits = list(envelope.get("hits") or [])
            if hits:
                selected = hits if self.selected_from_receptor_id else self.store.gate.take(hits)
                if self.hit_mode != HIT_MODE_TOP_N or self.selected_from_receptor_id:
                    self._materialize_selected(selected)
            hit_count = int(envelope.get("hits_found", len(hits)) or 0)
            if shard_id > 0:
                recorded = advance_shard_run(
                    self.project_db,
                    run_id=self.run_id,
                    shard_id=shard_id,
                    receptor_id=int(envelope.get("receptor_id") or 0),
                    engine=self.engine,
                    protocol_hash=self.protocol_hash,
                    scored=int(envelope.get("scored") or 0),
                    hits=hit_count,
                    result_path=_project_relative(str(envelope.get("result_shard") or "")),
                )
                if recorded:
                    advance_target(
                        self.project_db,
                        run_id=self.run_id,
                        receptor_id=int(envelope.get("receptor_id") or 0),
                        scored=int(envelope.get("scored") or 0),
                        hits=hit_count,
                    )

    def on_error(self, chunk_id: str, error: str) -> None:
        """MF owns the failure; without a completed receipt a resubmission runs it again."""

    def flush(self) -> None:
        """Nothing to flush mid-run: streaming mode already wrote, ranked mode must not yet."""

    def close(self) -> None:
        """Finalize hits on success; expose cancellation/failure without promoting partial top-N."""
        if self.terminal_status == "completed":
            if self.hit_mode == HIT_MODE_TOP_N and not self.selected_from_receptor_id:
                root = get_default_project_root()
                if root is None:
                    root = getattr(self.project_db, "project_dir", None)
                if root is None:
                    raise RuntimeError("Cannot resolve the project root for ranked result selection.")
                receptor_ids = sorted({
                    int(receipt.receptor_molecule_id)
                    for receipt in completed_result_receipts(
                        self.project_db, run_id=self.run_id, protocol_hash=self.protocol_hash
                    )
                })
                selected = [
                    hit
                    for receptor_id in receptor_ids
                    for hit in select_hits(
                        self.project_db,
                        project_root=root,
                        run_id=self.run_id,
                        protocol_hash=self.protocol_hash,
                        filters={
                            "score__lte": self.hit_threshold,
                            "receptor_id": receptor_id,
                        },
                        top_n=self.hit_cap,
                    )
                ]
                self._materialize_selected(selected)
            finish_run(self.project_db, run_id=self.run_id)
            return
        stop_run(
            self.project_db,
            run_id=self.run_id,
            state=TargetState.CANCELED if self.terminal_status == "canceled" else TargetState.FAILED,
        )

    def on_job_terminal(self, status: str) -> None:
        self.terminal_status = str(status or "failed")


def shard_hit_writer(
    *,
    project_db: Any,
    hit_cap: int,
    hit_threshold: float,
    payload: str = PAYLOAD_LIGHT,
    hit_mode: str = HIT_MODE_THRESHOLD,
    run_id: str = "",
    engine: str = "",
    protocol_hash: str = "",
    selected_from_receptor_id: int | None = None,
) -> ShardHitWriter:
    """Factory for `result_handler_factory`; the runtime passes the kwargs from the API."""
    return ShardHitWriter(
        project_db=project_db, hit_cap=hit_cap, hit_threshold=hit_threshold,
        payload=payload, hit_mode=hit_mode, run_id=run_id,
        engine=engine, protocol_hash=protocol_hash,
        selected_from_receptor_id=selected_from_receptor_id,
    )


def _project_relative(path: str) -> str:
    text = str(path or "")
    root = get_default_project_root()
    if not text or root is None or not Path(text).is_absolute():
        return text
    with suppress(ValueError):
        return str(Path(text).relative_to(root))
    return text


def receptor_targets(project_db, parsed: DockShardsJobParams) -> list[dict[str, Any]]:
    """The prepared receptors with a grid, as everything a chunk needs to dock against one."""
    rows = list_entity_rows(
        project_db,
        entity_kind="receptor",
        engine=parsed.preparation_engine,
        set_id=parsed.receptor_set_id,
        filters={**dict(parsed.receptor_filters or {}), "prepared_engine": True},
        fields=(
            "id", "name", "stored_path", "current_path", "input_format", "metadata_json",
            "prepared_engine_path", "prepared_files", "grid_engine_payload",
        ),
        order=("source", "source_index"),
    )
    targets: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        grid = grid_from_row(row, engine=parsed.preparation_engine) or {}
        center = [float(v) for v in (parsed.box_center or grid.get("center") or ())]
        size = [float(v) for v in (parsed.box_size or grid.get("size") or ())]
        if len(center) != 3 or len(size) != 3:
            continue  # no binding site: not dockable, and a campaign is not the place to guess
        targets.append({
            "receptor_id": int(row.get("id") or 0),
            "receptor_name": str(row.get("name") or ""),
            "receptor_path": str(docking_input_path_from_row(row, engine=parsed.preparation_engine)),
            "box_center": center,
            "box_size": size,
            "spacing": float(grid.get("spacing") or parsed.spacing),
        })
    return targets


def _shard_docking_scope(
    project_db,
    parsed: DockShardsJobParams,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[int, int]]]:
    """Resolve the immutable shard/receptor pairs shared by counting and dispatch."""
    targets = receptor_targets(project_db, parsed)
    if parsed.selected_from_receptor_id:
        targets = [
            target for target in targets
            if int(target.get("receptor_id") or 0) != int(parsed.selected_from_receptor_id)
        ]
    shards = [
        row
        for row in iter_prepared_shards(project_db, engine=parsed.preparation_engine)
        if int(row.get("n_records") or 0) > 0
        and Path(str(row.get("prepared_path") or "")).is_file()
    ]
    if parsed.selected_from_receptor_id:
        db_path = getattr(project_db, "db_path", None)
        root = (
            get_default_project_root()
            or getattr(project_db, "project_dir", None)
            or (Path(db_path).parent if db_path else None)
        )
        if root is None:
            raise RuntimeError("Cannot resolve the project root for off-target Top-N selection.")
        selected = select_hit_refs(
            project_db,
            project_root=root,
            run_id=parsed.run_id,
            protocol_hash=str(parsed.protocol_metadata.hash or ""),
            filters={
                "receptor_id": int(parsed.selected_from_receptor_id),
                "score__lte": float(parsed.hit_threshold),
            },
            top_n=int(parsed.selected_top_n or parsed.hit_cap),
        )
        record_ids_by_shard: dict[int, list[int]] = {}
        for row in selected:
            record_ids_by_shard.setdefault(int(row["_shard_id"]), []).append(int(row["ligand_id"]))
        shards = [
            {
                **row,
                "record_ids": sorted(set(record_ids_by_shard[int(row.get("id") or 0)])),
                "n_records": len(set(record_ids_by_shard[int(row.get("id") or 0)])),
            }
            for row in shards
            if int(row.get("id") or 0) in record_ids_by_shard
        ]
    protocol_hash = str(parsed.protocol_metadata.hash or "")
    completed = (
        completed_shard_targets(
            project_db,
            run_id=parsed.run_id,
            engine=parsed.engine,
            protocol_hash=protocol_hash,
        )
        if parsed.skip_existing
        else set()
    )
    if parsed.check_required and not shards and not parsed.selected_from_receptor_id:
        raise RuntimeError(
            "docking.run found no prepared shard artifacts for engine family "
            f"'{parsed.preparation_engine}'. Run docking.prepare_ligands(...) first."
        )
    if parsed.check_required and not targets:
        raise RuntimeError("docking.run found no prepared receptors with a binding site.")
    return targets, shards, completed


class DockShardsJobSpec(JobSpec):
    name = "amdock_dock_shards_job"
    task_name = "amdock_dock_shard_slice"
    description = "Dock a sharded screening library, one whole shard per receptor at a time."
    params_model = DockShardsJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    # No output_spec: hits become molecules before they can become results, and only the
    # parent (holding the gate) may decide that. `ShardHitWriter` is the sink.
    result_handler_factory = shard_hit_writer
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def count_chunks(params: dict, config: dict | None = None) -> int:
        """Declare the whole pending campaign scope before MF opens its lazy feed."""
        parsed = DockShardsJobParams(**params)
        project_db = dict(config or {}).get("project_db")
        if project_db is None:
            raise ValueError("dock_shards_job requires project_db in config.")
        targets, shards, completed = _shard_docking_scope(project_db, parsed)
        pending = sum(
            (int(shard.get("id") or 0), int(target["receptor_id"])) not in completed
            for shard in shards
            for target in targets
        )
        # build_chunks emits one no-op sentinel for an already-complete/empty scope.
        return max(1, pending)

    @staticmethod
    def run_chunk(payload: dict, progress_cb=None):
        shards = list(payload.get("shards") or [])
        total = max(1, sum(max(0, int(shard.get("n_records") or 0)) for shard in shards))
        completed = 0
        envelopes: list[dict] = []
        for shard in shards:
            shard_total = max(0, int(shard.get("n_records") or 0))

            def report(done: int, *, before: int = completed) -> None:
                if progress_cb is not None:
                    progress_cb(min(99.0, 100.0 * (before + int(done)) / total))

            envelopes.append(DockShardsJobSpec._dock_one_shard(payload, shard, progress_cb=report))
            completed += shard_total
        return envelopes

    @staticmethod
    def _dock_one_shard(payload: dict, shard: dict, progress_cb=None) -> dict:
        """One whole shard against the chunk's receptor: extract, dock, keep the hits.

        Split out of `run_chunk` because a chunk may carry several shards and each one is its
        own engine call — the ligand ids inside a shard start at 0, so two shards in one call
        would write over each other's poses.
        """
        source = str(shard.get("source") or "")
        shard_index = int(shard.get("shard_index") or 0)
        envelope = {
            "shard_id": int(shard.get("shard_id") or 0),
            "source": source,
            "shard_index": shard_index,
            "chunks_total": int(payload.get("chunks_total") or 1),
            "receptor_id": int(payload.get("receptor_id") or 0),
            # What the campaign progress counts: dockings finished, hit or not.
            "scored": 0,
            "hits": [],
            "hits_found": 0,
            "result_shard": "",
        }
        shard_path = str(shard.get("shard_path") or "")
        if not shard_path:
            return envelope
        output_dir = Path(str(payload["output_dir"])).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        threshold = float(payload["hit_threshold"])
        result_path = result_shard_path(output_dir, payload, shard)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ligands_", dir=str(output_dir)) as staging:
            ligands = extract_ligands(
                shard_path,
                staging,
                record_ids=shard.get("record_ids"),
            )
            smiles = {int(ligand["id"]): str(ligand["smiles"]) for ligand in ligands}
            pairs = [
                {
                    "run_kind": "screening",
                    "ligand_id": int(ligand["id"]),
                    "receptor_id": int(payload["receptor_id"]),
                    "ligand_path": ligand["path"],
                    "receptor_path": str(payload["receptor_path"]),
                    "exhaustiveness": int(payload.get("exhaustiveness") or 8),
                    "num_modes": int(payload.get("num_modes") or 9),
                    "box_center": list(payload.get("box_center") or []),
                    "box_size": list(payload.get("box_size") or []),
                    "spacing": float(payload.get("spacing") or 0.375),
                }
                for ligand in ligands
            ]
            with Shard.open(shard_path) as source_shard:
                header = {
                    "dataset_id": source_shard.dataset_id,
                    "shard_id": source_shard.shard_id,
                    "base_id": source_shard.base_id,
                    "slot_count": source_shard.slot_count,
                }
            processed = 0
            hits: list[dict] = []
            hits_found = 0
            try:
                with ResultFragmentWriter(
                    result_path,
                    metadata={
                        "dataset_id": header["dataset_id"],
                        "shard_id": header["shard_id"],
                        "base_id": header["base_id"],
                        "slot_count": header["slot_count"],
                        "source_shard": source,
                        "run_id": str(payload.get("run_id") or ""),
                        "protocol": dict(payload.get("protocol_metadata") or {}),
                        "receptor_id": envelope["receptor_id"],
                        "selection": {
                            "mode": str(payload.get("hit_mode") or HIT_MODE_THRESHOLD),
                            "cap": int(payload.get("hit_cap") or 0),
                            "threshold": threshold,
                            **(
                                {"selected_from_receptor_id": int(payload["selected_from_receptor_id"])}
                                if payload.get("selected_from_receptor_id") else {}
                            ),
                        },
                    },
                ) as writer:
                    def consume_pair(pair_rows: list[dict]) -> None:
                        nonlocal hits_found, processed
                        if not pair_rows:
                            return
                        row = next(
                            (item for item in pair_rows if int(item.get("pose_rank") or 1) == 1),
                            pair_rows[0],
                        )
                        ligand_id = int(row.get("ligand_molecule_id") or 0)
                        stored = result_row(row, smiles=smiles.get(ligand_id, ""))
                        writer.add(stored)
                        is_hit = (
                            row.get("score") is not None
                            and bool(stored.get("has_pose"))
                            and (
                                bool(payload.get("selected_from_receptor_id"))
                                or float(row["score"]) <= threshold
                            )
                        )
                        if is_hit:
                            hits_found += 1
                            if (
                                str(payload.get("hit_mode") or HIT_MODE_THRESHOLD) != HIT_MODE_TOP_N
                                or payload.get("selected_from_receptor_id")
                            ):
                                hit = _hit_row(
                                    row,
                                    smiles=smiles.get(ligand_id, ""),
                                    source=source,
                                    shard_index=shard_index,
                                )
                                hit["pose_path"] = ""
                                hit["metrics"] = {
                                    **dict(hit.get("metrics") or {}),
                                    "selected_pose_path": "",
                                    "selected_pose_pdbqt_path": "",
                                }
                                hit["_pose_text"] = stored["pose_text"]
                                hit["_pose_suffix"] = stored["pose_suffix"]
                                hit["_result_shard"] = str(result_path)
                                hit["_result_record_id"] = ligand_id
                                hit["_shard_id"] = int(shard.get("shard_id") or 0)
                                hits.append(hit)
                        # The result shard is now the durable copy. No engine-specific loose pose
                        # survives the callback; selected hits are restored by the parent later.
                        _discard_poses([row])
                        processed += 1
                        if progress_cb is not None:
                            progress_cb(processed)

                    rows = run_docking_chunk({
                        **payload,
                        "pairs": pairs,
                        "report_name": f"shard_{shard_index:05d}_r{envelope['receptor_id']:05d}.json",
                        "_pair_callback": consume_pair,
                        "_collect_rows": False,
                    })
                    # Registered engines predating the streaming callback still work; they merely
                    # flush at the end of their engine call until they adopt the callback contract.
                    if rows:
                        by_ligand: dict[int, list[dict]] = {}
                        for row in rows:
                            by_ligand.setdefault(int(row.get("ligand_molecule_id") or 0), []).append(row)
                        for pair_rows in by_ligand.values():
                            consume_pair(pair_rows)
            except BaseException:
                # A canceled/failed task has no receipt; do not leave interactive pose files
                # for work that will be retried as a whole shard.
                _discard_poses(hits)
                raise
        envelope["scored"] = processed
        envelope["hits"] = hits
        envelope["hits_found"] = hits_found
        envelope["result_shard"] = str(result_path)
        return envelope

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = DockShardsJobParams(**params)
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("dock_shards_job requires project_db in config.")
        output_dir = resolve_docking_output_dir(params, config_map) / SCREENING_SUBDIR
        targets, prepared_rows, completed = _shard_docking_scope(project_db, parsed)
        prepared_records = sum(int(row.get("n_records") or 0) for row in prepared_rows)
        # Chunk construction is deferred by MF while preparation is active. Resolve and plan
        # here, not in the public API, so totals describe the completed preparation snapshot.
        if parsed.run_id:
            plan_targets(
                project_db,
                run_id=parsed.run_id,
                receptors=targets,
                ligands_total=prepared_records,
                engine=parsed.engine,
            )
        emitted = False
        for row in prepared_rows:
            shard = {
                "shard_id": int(row.get("id") or 0),
                "shard_path": worker_file(Path(str(row.get("prepared_path") or ""))),
                "source": str(row.get("source") or ""),
                "shard_index": int(row.get("shard_index") or 0),
                "n_records": int(row.get("n_records") or 0),
                **({"record_ids": list(row.get("record_ids") or ())} if row.get("record_ids") is not None else {}),
            }
            if not targets:
                break
            # The cap half of the gate, as in `vs`: the feed stops handing out work once the
            # run has written its quota. Checked per group, so chunks in flight can overshoot;
            # the exact ceiling is the gate in ShardHitWriter.
            if (
                parsed.hit_mode != HIT_MODE_TOP_N
                and count_docking_results(project_db, run_id=parsed.run_id, score_lte=parsed.hit_threshold)
                >= parsed.hit_cap
            ):
                break
            for target in targets:
                if (int(shard.get("shard_id") or 0), int(target["receptor_id"])) in completed:
                    continue
                flex = Path(target["receptor_path"]).with_name(
                    f"{Path(target['receptor_path']).stem}__flex.pdbqt"
                )
                emitted = True
                yield {
                    "shards": [shard],
                    # A shard is whole inside one chunk, so it is docked once every receptor
                    # has reported back — that is what moves it to `docked`.
                    "chunks_total": len(targets),
                    "output_dir": worker_output_dir(output_dir),
                    "receptor_id": target["receptor_id"],
                    "receptor_path": worker_file(target["receptor_path"], cache=True),
                    **({"flex_receptor_path": worker_file(flex, cache=True)} if flex.is_file() else {}),
                    "box_center": target["box_center"],
                    "box_size": target["box_size"],
                    "spacing": target["spacing"],
                    "engine": parsed.engine,
                    "exhaustiveness": parsed.exhaustiveness,
                    "num_modes": parsed.num_modes,
                    "scoring_function": parsed.scoring_function,
                    "vina_backend": parsed.vina_backend,
                    "vina_command": parsed.vina_command,
                    "vina_cpu": parsed.vina_cpu,
                    "seed": parsed.seed,
                    "energy_range": parsed.energy_range,
                    "min_rmsd": parsed.min_rmsd,
                    "run_id": parsed.run_id,
                    "protocol_metadata": parsed.protocol_metadata.as_metrics_payload(),
                    "engine_config": dict(parsed.engine_config),
                    **chunk_resources(parsed.engine, parsed.engine_config),
                    "hit_threshold": parsed.hit_threshold,
                    "hit_cap": parsed.hit_cap,
                    "hit_mode": parsed.hit_mode,
                    "selected_from_receptor_id": parsed.selected_from_receptor_id,
                }
        if not emitted:
            # MF needs one chunk to close the job out when there is nothing to dock.
            yield {"shards": [], "chunks_total": 1}


dock_shards_job = DockShardsJobSpec.to_job_definition()


__all__ = [
    "DockShardsJobParams",
    "DockShardsJobSpec",
    "RESULT_SHARD_FORMAT",
    "SCREENING_SUBDIR",
    "ShardHitWriter",
    "dock_shards_job",
    "recover_ranked_hits",
    "recoverable_ranked_runs",
    "result_shard_path",
    "select_hits",
    "shard_hit_writer",
]
