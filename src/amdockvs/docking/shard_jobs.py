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

from pathlib import Path
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import select

from ms_flow.core.data.exclusions import active_exclusions

from amdockvs.core.jobs import JobSpec
from amdockvs.docking.shard_tasks import HIT_MODE_TOP_N, dock_shards, top_n_hits

from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS, DEFAULT_LOCAL_CPU_EXECUTOR, DEFAULT_VINA_COMMAND
from amdockvs.docking.engines.programs import chunk_resources
from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.repository import inputs_dir, list_entity_rows, resolve_docking_output_dir, with_input_files
from amdockvs.docking.results.repository import docking_run_id, find_docking_run, runs_where
from amdockvs.docking.results.dataset import (
    UNIQUE_BY,
    docked_pairs,
    hit_refs,
    key_files,
    off_target_selection,
    result_fragment_path,
    result_metadata,
    result_roles,
    run_counts,
    select_hit_refs,
    select_hits,
    state_key,
)
from amdockvs.docking.methods import excludes
from amdockvs.docking.pairs import docking_input_key, site_targets
from amdockvs.docking.preparation.state import grid_from_row
from amdockvs.docking.rerun import supersede_shard_runs
from amdockvs.screening.campaign import (
    advance_target,
    done_shards,
    finish_run,
    plan_targets,
    record_docked,
    stop_run,
    target_keys,
)
from amdockvs.screening.materialize import HitGate, PAYLOAD_LIGHT, ingest_hits
from amdockvs.models import DockingResultRecord, DockingRun, ScreeningTarget
from amdockvs.molecules.storage import ShardStore, iter_prepared_shards
from amdockvs.core.vocab import TargetState

# Poses of a campaign live apart from `vs` results: shard record ids and molecule ids both start
# at 0, and the pose file name is built from the ligand id.
SCREENING_SUBDIR = "screening"

# The prefilter a ranked run uses: none. Docking scores are negative, so this only drops poses
# that did not bind at all — "best N of the whole run" must not quietly exclude what the user
# never set a cutoff for.
RANKED_PREFILTER_SCORE = 0.0



def result_shard_path(output_dir: str | Path, payload: dict, shard: dict) -> Path:
    """Compatibility name for the Parquet result-fragment path."""
    return result_fragment_path(output_dir, payload, shard)


def _public(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hits as `ingest_hits` takes them: without the keys that located them in their result
    shard. Each already carries its pose, compressed where it was read (a worker)."""
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _count_imported(project_db, docking_run_id: int | None, rows: list[dict[str, Any]]) -> None:
    """The `imported` counters of the receptors of the materialized hits, counted again from the
    results: hits that come twice (a campaign submitted again, a replay) are counted once."""
    run = int(docking_run_id or 0)
    receptor_ids = {int(row.get("receptor_molecule_id") or 0) for row in rows}
    with project_db.get_session() as session:
        for target in session.exec(select(ScreeningTarget).where(
                ScreeningTarget.docking_run_id == run, ScreeningTarget.receptor_molecule_id.in_(receptor_ids))).all():
            target.imported = int(session.exec(select(func.count()).select_from(DockingResultRecord).where(
                DockingResultRecord.docking_run_id == run,
                DockingResultRecord.receptor_molecule_id == target.receptor_molecule_id)).one())
            session.add(target)
        session.commit()


def _materialized_count(project_db, *, run_id: str, protocol_hash: str) -> int:
    with project_db.get_session() as session:
        statement = select(func.count()).select_from(DockingResultRecord).where(
            DockingResultRecord.docking_run_id.in_(runs_where(run_id=str(run_id), protocol_hash=str(protocol_hash))),
        )
        return int(session.exec(statement).one())


def recoverable_ranked_runs(project_db, *, project_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Terminal ranked campaigns whose completed result shards have not been materialized."""
    with project_db.get_session() as session:
        targets = session.exec(select(ScreeningTarget).where(ScreeningTarget.state_keys != "")).all()
        target_states: dict[int, set[str]] = {}
        for target in session.exec(select(ScreeningTarget)).all():
            target_states.setdefault(int(target.docking_run_id), set()).add(str(target.state))
        submit_ids = {int(row.id): (str(row.run_id or ""), str(row.protocol_hash or ""))
                      for row in session.exec(select(DockingRun)).all()}

    groups: dict[int, list[ScreeningTarget]] = {}
    for target in targets:
        groups.setdefault(int(target.docking_run_id), []).append(target)

    candidates: list[dict[str, Any]] = []
    active = {TargetState.SCHEDULED, TargetState.RUNNING}
    for run, rows in groups.items():
        run_id, protocol_hash = submit_ids.get(run, ("", ""))
        if target_states.get(run, set()) & active:
            continue
        if _materialized_count(project_db, run_id=run_id, protocol_hash=protocol_hash):
            continue
        keys = target_keys(project_db, [run])
        paths = key_files(project_db, keys)
        if not paths:
            continue
        metadata = result_metadata(paths[0])
        selection = dict(metadata.get("selection") or {})
        mode = str(selection.get("mode") or "unknown")
        if mode not in {HIT_MODE_TOP_N, "unknown"} or selection.get("selected_from_receptor_id"):
            continue  # an off-target stage streams its hits: nothing to recover
        protocol = dict(metadata.get("protocol") or {})
        shards, scored = run_counts(project_db, keys, run)
        candidates.append({
            "run_id": run_id,
            "protocol_hash": protocol_hash,
            "protocol_label": str(protocol.get("label") or protocol.get("program") or rows[0].engine),
            "status": ", ".join(sorted(target_states.get(run, {"terminal"}))),
            "top_n": max(0, int(selection.get("cap") or 0)),
            "threshold": float(selection.get("threshold", RANKED_PREFILTER_SCORE)),
            "shards": shards,
            "scored": scored,
            "docking_run_id": run,
        })
    return sorted(candidates, key=lambda row: row["docking_run_id"], reverse=True)


def recover_ranked_hits(
    runtime,
    *,
    run_id: str,
    protocol_hash: str,
    top_n: int,
    threshold: float = RANKED_PREFILTER_SCORE,
) -> dict[str, int]:
    """Rebuild a partial top-N from completed result shards, without rerunning docking: the Top-N
    job (`shard_top_n_job`), waited for, leaving the run's state as it is. Not from a thread that
    cannot wait."""
    project_db = runtime.workspace.project_db
    cap = int(top_n)
    if cap <= 0:
        raise ValueError("top_n must be greater than zero.")
    if _materialized_count(project_db, run_id=run_id, protocol_hash=protocol_hash):
        raise ValueError("This run/protocol already has materialized docking results.")

    with project_db.get_session() as session:
        # Whatever engine it ran under: the recovery dialog knows the run and its protocol only.
        run = session.exec(runs_where(run_id=run_id, run_kind="screening",
                                      protocol_hash=protocol_hash)).first()
    readable_shards, scanned = run_counts(project_db, target_keys(project_db, [run or 0]), run or 0)
    job = runtime.submit_job(shard_top_n_job, executor_name=DEFAULT_LOCAL_CPU_EXECUTOR, params=TopNJobParams(
        run_id=run_id, protocol_hash=protocol_hash, hit_cap=cap, hit_threshold=float(threshold),
        finish=False).model_dump(mode="python"))
    ended = runtime.wait_for_job(job)
    if ended.status != "completed":
        raise RuntimeError(f"The Top-N of run {run_id} ended {ended.status}: {ended.error or ''}".strip())
    return {
        "shards": readable_shards,
        "scored": scanned,
        "selected": _materialized_count(project_db, run_id=run_id, protocol_hash=protocol_hash),
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
    payload: str = PAYLOAD_LIGHT  # what a hit keeps of its pose
    # Set only for the dependent off-target stage. Once the reference job completes, its
    # durable ranking supplies the record ids; only those records are extracted from each shard.
    selected_from_receptor_id: int | None = Field(default=None, ge=1)
    selected_top_n: int | None = Field(default=None, ge=1)
    skip_existing: bool = True
    check_required: bool = True


class ShardHitWriter:
    """Parent-side sink: promote hits and mark the shards of each key done.

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
        run_id: str = "",
        engine: str = "",
        protocol_hash: str = "",
        selected_from_receptor_id: int | None = None,
        skip_existing: bool = True,
    ) -> None:
        self.project_db = project_db
        self.skip_existing = bool(skip_existing)
        self.run_id = str(run_id)
        self.engine = str(engine)
        self.protocol_hash = str(protocol_hash or "")
        self._docking_run_id: int | None = None
        self.hit_cap = int(hit_cap)
        self.hit_threshold = float(hit_threshold)
        self.selected_from_receptor_id = (
            int(selected_from_receptor_id) if selected_from_receptor_id else None
        )
        self.terminal_status = "completed"
        self._writers: dict[tuple[str, Any], Any] = {}  # (key, generation): its PairWriter, open over the run
        self.store = ShardStore(
            project_db,
            gate=HitGate(cap=self.hit_cap, threshold=self.hit_threshold),
            payload=payload,
        )

    @property
    def docking_run_id(self) -> int | None:
        """The run the feed created before its first chunk; None until then (nothing to count)."""
        if self._docking_run_id is None:
            self._docking_run_id = find_docking_run(
                self.project_db, run_id=self.run_id, engine=self.engine, run_kind="screening",
                protocol_hash=self.protocol_hash,
            )
        return self._docking_run_id

    def _materialize_selected(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        materialized = ingest_hits(
            self.project_db,
            _public(rows),
            gate=HitGate(cap=len(rows)),
            payload=self.store.payload,
            reuse_existing=True,
        )
        _count_imported(self.project_db, self.docking_run_id, materialized)
        return materialized

    def handle(self, chunk_id: str, result: Any) -> None:
        for envelope in list(result or []):
            shard_id = int(envelope.get("shard_id") or 0)
            hits = list(envelope.get("hits") or [])
            if hits and self.selected_from_receptor_id:  # only an off-target stage streams hits
                self._materialize_selected(hits)
            hit_count = int(envelope.get("hits_found", len(hits)) or 0)
            if shard_id > 0 and envelope.get("state_key"):
                key, generation = str(envelope["state_key"]), envelope.get("generation_id")
                result_path = str(envelope.get("result_shard") or "")
                recorded = record_docked(
                    self.project_db,
                    key=key,
                    shard_id=shard_id,
                    result_path=result_path,
                    # The task's id in this run: a replay of it is committed once.
                    part=f"{self.run_id}:{shard_id}" if self.run_id else "",
                    narrowed_by=str(envelope.get("narrowed_by") or ""),
                    generation_id=generation,
                    # A forced campaign docks what is already done: its rows must be stored, and
                    # a read keeps the newest per (shard, ligand).
                    force=not self.skip_existing,
                    writer=self._writer(key, generation, result_path) if result_path else None,
                    done_ids=envelope.get("done_ids"),
                    failed_ids=envelope.get("failed_ids"),
                )
                if recorded:
                    advance_target(
                        self.project_db,
                        docking_run_id=int(envelope.get("docking_run_id") or self.docking_run_id or 0),
                        receptor_id=int(envelope.get("receptor_id") or 0),
                        scored=int(envelope.get("scored") or 0),
                        hits=hit_count,
                    )

    def _writer(self, key: str, generation: Any, result_path: str):
        """The key's `PairWriter`, opened at its first part (the parts' folder's parent)."""
        from ms_flow.core.data.catalog import PairWriter

        if (key, generation) not in self._writers:
            self._writers[key, generation] = PairWriter(
                self.project_db, Path(result_path).parent.parent, generation_id=generation,
                roles=result_roles(key), prefix=f"{self.run_id}:" if self.run_id else "")
        return self._writers[key, generation]

    def on_error(self, chunk_id: str, error: str) -> None:
        """MF owns the failure; undone in the key's set, a resubmission runs it again."""

    def flush(self) -> None:
        """Nothing to flush mid-run: the top-N is picked when the run ends."""

    def close(self) -> None:
        """On success, supersede then retire; the run is finished by the Top-N job after it
        (`shard_top_n_job`), or here for an off-target stage, whose hits came as it went. Stopped,
        its receptors say so, and no partial Top-N is promoted."""
        completed = self.terminal_status == "completed"
        try:
            if completed:
                run = self.docking_run_id or 0
                keys = target_keys(self.project_db, [run])
                if not self.skip_existing:
                    # Forced: what this run docked again replaces what older runs held for those
                    # pairs. It reads that history, so it comes before the retire.
                    supersede_shard_runs(self.project_db, docking_run_id=run, keys=keys)
                for writer in self._writers.values():  # an older shard this run covers leaves the reads
                    writer.retire_covered(UNIQUE_BY[-1])
        finally:
            for writer in self._writers.values():  # no part comes after the last chunk: their locks go
                writer.close()
            self._writers.clear()
        if not completed:
            stop_run(
                self.project_db,
                docking_run_id=self.docking_run_id,
                state=TargetState.CANCELED if self.terminal_status == "canceled" else TargetState.FAILED,
            )
        elif self.selected_from_receptor_id:  # its hits came as it went: nothing comes after it
            finish_run(self.project_db, docking_run_id=self.docking_run_id)

    def on_job_terminal(self, status: str) -> None:
        self.terminal_status = str(status or "failed")


def shard_hit_writer(params: dict, config: dict) -> ShardHitWriter:
    """`result_handler_factory` of the docking of shards: the gate, from the run's params."""
    return ShardHitWriter(
        project_db=config["project_db"], hit_cap=params["hit_cap"], hit_threshold=params["hit_threshold"],
        payload=params["payload"], run_id=params["run_id"],
        engine=params["engine"], protocol_hash=str(params["protocol_metadata"].get("hash") or ""),
        selected_from_receptor_id=params["selected_from_receptor_id"],
        skip_existing=params.get("skip_existing", True),  # a forced campaign replaces when it ends
    )


def target_pair(shard_id: Any, target: Mapping[str, Any]) -> tuple[int, int, int]:
    """One piece of work: a shard, a receptor, and the receptor's site."""
    return (int(shard_id or 0), int(target.get("receptor_id") or 0),
            int(target.get("binding_site_id") or 0))


def receptor_targets(project_db, parsed: DockShardsJobParams, output_dir: Path) -> list[dict[str, Any]]:
    """The prepared receptors with a grid, as everything a chunk needs to dock against one: their
    files written once under the run's `output_dir`.

    One target per active binding site: a receptor with three of them is three targets, each
    with its own box, its own inputs and its own key. An explicit box overrides them."""
    rows = list_entity_rows(
        project_db,
        entity_kind="receptor",
        engine=parsed.preparation_engine,
        set_id=parsed.receptor_set_id,
        filters={**dict(parsed.receptor_filters or {}), "prepared_engine": True},
        fields=(
            "id", "name", "stored_payload_id", "current_payload_id", "metadata_json",
            "prepared_payload_id", "flex_payload_id", "grid_engine_payload", "binding_sites",
        ),
    )
    explicit_box = bool(parsed.box_center) and bool(parsed.box_size)
    targets: list[dict[str, Any]] = []
    for row in (rows if explicit_box else [t for row in rows for t in site_targets(row)]):
        row = dict(row)
        grid = grid_from_row(row, engine=parsed.preparation_engine) or {}
        center = [float(v) for v in (parsed.box_center or grid.get("center") or ())]
        size = [float(v) for v in (parsed.box_size or grid.get("size") or ())]
        if len(center) != 3 or len(size) != 3:
            continue  # no binding site: not dockable, and a campaign is not the place to guess
        files = with_input_files(project_db, row, inputs_dir(project_db))
        receptor_path = files["input_path"]
        if excludes(parsed.engine_config.get("method"), receptor_path, center, size):
            continue  # its box does not fit the docking method
        spacing = float(grid.get("spacing") or parsed.spacing)
        targets.append({
            "receptor_id": int(row.get("id") or 0),
            "binding_site_id": int(row.get("binding_site_id") or 0),
            "receptor_name": str(row.get("name") or ""),
            "receptor_path": receptor_path,
            "flex_receptor_path": files["flex_path"],
            "box_center": center,
            "box_size": size,
            "spacing": spacing,
            # What its hits are docked against, as `dock_pairs` stamps a row-fed run's results:
            # a later skip_existing run on the same ligands compares with it.
            "input_key": docking_input_key(files, center, size, spacing),
        })
    return targets


def _shard_docking_scope(
    project_db,
    parsed: DockShardsJobParams,
    output_dir: Path,
    unprepared: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[int, int]]]:
    """Resolve the immutable shard/receptor pairs shared by counting and dispatch. With
    `unprepared` (a campaign that prepares them there), a shard not prepared here is in it too."""
    protocol_hash = str(parsed.protocol_metadata.hash or "")
    selection = off_target_selection(parsed.selected_from_receptor_id, parsed.run_id)
    targets = [
        {**target, "state_key": state_key(
            engine=parsed.engine, protocol_hash=protocol_hash, receptor_id=target["receptor_id"],
            binding_site_id=target.get("binding_site_id") or 0,
            input_key=str(target.get("input_key") or ""), selection=selection,
        )}
        for target in receptor_targets(project_db, parsed, output_dir)
    ]
    reference_keys: set[str] = set()
    if parsed.selected_from_receptor_id:
        # Its library keys (selection ""), named before it leaves: an off-target stage ranks
        # what the reference receptor holds, and does not dock it again.
        reference_keys = {
            state_key(engine=parsed.engine, protocol_hash=protocol_hash,
                      receptor_id=target["receptor_id"],
                      binding_site_id=target.get("binding_site_id") or 0,
                      input_key=str(target.get("input_key") or ""))
            for target in targets
            if int(target.get("receptor_id") or 0) == int(parsed.selected_from_receptor_id)
        }
        targets = [
            target for target in targets
            if int(target.get("receptor_id") or 0) != int(parsed.selected_from_receptor_id)
        ]
    shards = [
        row
        for row in iter_prepared_shards(project_db, engine=parsed.preparation_engine, unprepared=unprepared)
        if int(row.get("n_records") or 0) > 0
        and (not row["prepared_path"] or Path(row["prepared_path"]).is_file())
    ]
    if parsed.selected_from_receptor_id:
        selected = select_hit_refs(
            project_db,
            # The reference receptor's library keys, over every run that filled them: the stage
            # ahead of this one skips what another run already docked, so ranking only what this
            # submission redid would select nothing.
            keys=reference_keys,
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
    # A shard the exclusions left partial is docked partially, and its done-set entry says so,
    # or undoing the cut would skip the rest of it as already docked.
    narrowed = {
        int(row.get("id") or 0) for row in shards
        if row.get("record_ids") is not None and not parsed.selected_from_receptor_id
    }
    exclusion_key = active_exclusions(project_db).key if narrowed else ""
    for row in shards:
        row["narrowed_by"] = exclusion_key if int(row.get("id") or 0) in narrowed else ""
    completed: set[tuple[int, int, int]] = set()
    if parsed.skip_existing:
        # The key spans every run of this engine, protocol, receptor site and input: a campaign
        # gets a fresh `run_id` each time it is submitted, so skipping by run would skip nothing
        # and dock the library again beside its own results. Whole shards only — a subset docked
        # under the exclusions leaves the rest of its shard undocked, and comes back with them.
        for target in targets:
            done = done_shards(project_db, target["state_key"])
            if narrowed:
                done |= done_shards(project_db, target["state_key"], narrowed_by=exclusion_key) & narrowed
            completed |= {target_pair(shard, target) for shard in done}
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
    supported_executors = AMDOCKVS_OFFLOADABLE_EXECUTORS
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
        output_dir = resolve_docking_output_dir(params, dict(config or {})) / SCREENING_SUBDIR
        targets, shards, completed = _shard_docking_scope(project_db, parsed, output_dir)
        pending = sum(
            target_pair(shard.get("id"), target) not in completed
            for shard in shards
            for target in targets
        )
        # build_chunks emits one no-op sentinel for an already-complete/empty scope.
        return max(1, pending)

    run_chunk = staticmethod(dock_shards)


    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = DockShardsJobParams(**params)
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("dock_shards_job requires project_db in config.")
        output_dir = resolve_docking_output_dir(params, config_map) / SCREENING_SUBDIR
        # `unprepared`: what the campaign prepares there (`amdockvs.docking.campaign`), never a job here.
        targets, prepared_rows, completed = _shard_docking_scope(project_db, parsed, output_dir,
                                                                 bool(config_map.get("unprepared")))
        prepared_records = sum(int(row.get("n_records") or 0) for row in prepared_rows)
        pending = [
            (row, target)
            for row in prepared_rows
            for target in targets
            if target_pair(row.get("id"), target) not in completed
        ]
        # Chunk construction is deferred by MF while preparation is active. Resolve and plan
        # here, not in the public API, so totals describe the completed preparation snapshot.
        # A run with nothing left to dock gets no row: it docked nothing, and an empty run of
        # its own would show in Results as a campaign that ran.
        run = docking_run_id(
            project_db, run_id=parsed.run_id, engine=parsed.engine, run_kind="screening",
            protocol=parsed.protocol_metadata.as_metrics_payload(),
        ) if pending else None
        if run is not None and parsed.run_id:
            # Done counts inside the scope: each receptor starts from the records of the shards
            # another run of this protocol already docked against it — none, after a forced one.
            plan_targets(
                project_db,
                docking_run_id=run,
                receptors=[
                    {**target, "ligands_done": sum(
                        int(row.get("n_records") or 0)
                        for row in prepared_rows
                        if target_pair(row.get("id"), target) in completed
                    ), "ligands_total": prepared_records * sum(
                        1 for other in targets
                        if int(other["receptor_id"]) == int(target["receptor_id"]))}
                    for target in targets
                ],
                ligands_total=prepared_records,
                engine=parsed.engine,
            )
        emitted = False
        for row in prepared_rows:
            shard = {
                "shard_id": int(row.get("id") or 0),
                "shard_path": worker_file(row["prepared_path"]) if row["prepared_path"] else "",
                # A prepared file holds many shards: `prepared_shard` is this one's number in it.
                **({"prepared_shard": row.get("prepared_shard")} if row["prepared_path"]
                   else {"molecules": worker_file(row["path"]), "shard": row.get("shard")}),
                "import_batch_id": row.get("scope"),
                "library": str(row.get("library") or ""),
                "shard_index": int(row.get("shard_index") or 0),
                "n_records": int(row.get("n_records") or 0),
                **({"record_ids": list(row.get("record_ids") or ())} if row.get("record_ids") is not None else {}),
                # What the parent records when it comes back: under which cuts it was docked,
                # and of which generation its id is.
                "narrowed_by": str(row.get("narrowed_by") or ""),
                "generation_id": row.get("generation_id"),
            }
            if not targets:
                break
            for target in targets:
                if target_pair(shard.get("shard_id"), target) in completed:
                    continue
                emitted = True
                yield {
                    "shards": [shard],
                    # A shard is whole inside one chunk, so it is docked once every receptor
                    # has reported back — that is what moves it to `docked`.
                    "chunks_total": len(targets),
                    "output_dir": worker_output_dir(output_dir),
                    "receptor_id": target["receptor_id"],
                    "binding_site_id": target.get("binding_site_id") or 0,
                    "receptor_path": target["receptor_path"],
                    **({"flex_receptor_path": target["flex_receptor_path"]} if target.get("flex_receptor_path") else {}),
                    "box_center": target["box_center"],
                    "box_size": target["box_size"],
                    "spacing": target["spacing"],
                    "input_key": target.get("input_key", ""),
                    # Where this piece of work's results live and what it marks done.
                    "state_key": target["state_key"],
                    "engine": parsed.engine,
                    "exhaustiveness": parsed.exhaustiveness,
                    "num_modes": parsed.num_modes,
                    "scoring_function": parsed.scoring_function,
                    "vina_command": parsed.vina_command,
                    "vina_cpu": parsed.vina_cpu,
                    "seed": parsed.seed,
                    "energy_range": parsed.energy_range,
                    "min_rmsd": parsed.min_rmsd,
                    "run_id": parsed.run_id,
                    "docking_run_id": run,
                    "protocol_metadata": parsed.protocol_metadata.as_metrics_payload(),
                    "engine_config": dict(parsed.engine_config),
                    **chunk_resources(parsed.engine, parsed.engine_config),
                    "hit_threshold": parsed.hit_threshold,
                    "hit_cap": parsed.hit_cap,
                    "selected_from_receptor_id": parsed.selected_from_receptor_id,
                }
        if not emitted:
            # MF needs one chunk to close the job out when there is nothing to dock.
            yield {"shards": [], "chunks_total": 1}


dock_shards_job = DockShardsJobSpec.to_job_definition()


class TopNJobParams(BaseModel):
    """The run whose Top-N becomes rows, and its gate. `finish`: the run is finished when this
    ends (False for a recovery, which leaves the run's state as it is)."""

    run_id: str
    engine: str = ""
    protocol_hash: str = ""
    hit_cap: int = Field(ge=1)
    hit_threshold: float = RANKED_PREFILTER_SCORE
    payload: str = PAYLOAD_LIGHT
    finish: bool = True


def _top_n_run(project_db, params: TopNJobParams) -> int | None:
    """The run of `params`, under whichever engine when it names none (a recovery knows the run
    and its protocol only)."""
    with project_db.get_session() as session:
        return session.exec(runs_where(run_id=params.run_id, engine=params.engine or None, run_kind="screening",
                                       protocol_hash=params.protocol_hash or None)).first()


class TopNWriter:
    """Parent-side sink of `shard_top_n_job`: the hits it gets, poses already compressed, become
    rows (once each: a redo reuses what it finds), and the run is finished when the job ends."""

    def __init__(self, *, project_db: Any, params: TopNJobParams) -> None:
        self.project_db, self.params, self.terminal_status = project_db, params, "completed"

    @property
    def docking_run_id(self) -> int | None:
        return _top_n_run(self.project_db, self.params)

    def handle(self, chunk_id: str, result: Any) -> None:
        hits = _public(list((result or {}).get("hits") or []))
        if hits:
            materialized = ingest_hits(self.project_db, hits, gate=HitGate(cap=len(hits)),
                                       payload=self.params.payload, reuse_existing=True)
            _count_imported(self.project_db, self.docking_run_id, materialized)

    def flush(self) -> None:
        """Every chunk wrote what it brought."""

    def on_job_terminal(self, status: str) -> None:
        self.terminal_status = str(status or "failed")

    def close(self) -> None:
        if not self.params.finish:
            return
        if self.terminal_status == "completed":
            finish_run(self.project_db, docking_run_id=self.docking_run_id)
            return
        stop_run(self.project_db, docking_run_id=self.docking_run_id,
                 state=TargetState.CANCELED if self.terminal_status == "canceled" else TargetState.FAILED)


def top_n_writer(params: dict, config: dict) -> TopNWriter:
    return TopNWriter(project_db=config["project_db"], params=TopNJobParams(**params))


class TopNJobSpec(JobSpec):
    """The final Top-N of a ranked run, after its docking job: the parent ranks the summaries (refs,
    one chunk per receptor), a worker reads the poses of those and compresses them, the parent
    inserts. A killed one is done again whole: what it inserted is found, not duplicated."""

    name = "amdock_shard_top_n_job"
    task_name = "amdock_shard_top_n"
    description = "Turn the best docked ligands of a ranked run into result rows."
    params_model = TopNJobParams
    executor = "compute"
    result_handler_factory = top_n_writer
    store_results = False
    required = ()
    produces = ()
    run_chunk = staticmethod(top_n_hits)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = TopNJobParams(**params)
        project_db = dict(config or {})["project_db"]
        run = _top_n_run(project_db, parsed) or 0
        with project_db.get_session() as session:
            receptor_ids = sorted({int(row) for row in session.exec(select(ScreeningTarget.receptor_molecule_id).where(
                ScreeningTarget.docking_run_id == run, ScreeningTarget.state_keys != "")).all()})
        emitted = False
        for receptor_id in receptor_ids:
            refs = hit_refs(project_db, keys=target_keys(project_db, [run], receptor_id=receptor_id),
                            filters={"score__lte": parsed.hit_threshold, "receptor_id": receptor_id},
                            top_n=parsed.hit_cap)
            if refs["hits"]:
                emitted = True
                yield refs
        if not emitted:  # MF needs one chunk to close the job out
            yield {"hits": [], "payloads": []}


shard_top_n_job = TopNJobSpec.to_job_definition()


__all__ = [
    "DockShardsJobParams",
    "DockShardsJobSpec",
    "SCREENING_SUBDIR",
    "ShardHitWriter",
    "TopNJobParams",
    "TopNJobSpec",
    "dock_shards_job",
    "recover_ranked_hits",
    "recoverable_ranked_runs",
    "result_shard_path",
    "select_hits",
    "shard_hit_writer",
    "shard_top_n_job",
]
