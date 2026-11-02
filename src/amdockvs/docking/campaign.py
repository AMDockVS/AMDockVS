"""Docking a sharded library on a cluster: the whole screening is one job of its queue.

    vs    every prepared shard against the receptors, its fragments left there; ranked there
    hits  the poses of the best `hit_cap` of each receptor, read from those fragments
    off   with off-targets: the reference receptor's best against each of them

What comes back is the receipts of `vs` (the progress), the hits and the off-target hits: what
`dock_shards_job` would have written here, without a fragment travelling. The campaign is an
ms_flow job (`ms_flow.campaign`): submitted again, with another run id too, it is found where it
is by its identity, and what comes again is written once — receipts and hits are upserts, the
counters are counted again, not added to, and under a new run id the hits move to that run.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from pydantic import Field
from ms_flow.campaign import From, Stage, campaign

from amdockvs.core.jobs import configs
from amdockvs.core.vocab import TargetState
from amdockvs.core.worker_io import worker_file
from amdockvs.docking.repository import resolve_docking_output_dir
from amdockvs.docking.results.repository import find_docking_run, supersede_results
from amdockvs.docking.shard_jobs import (
    SCREENING_SUBDIR, DockShardsJobParams, DockShardsJobSpec, ShardHitWriter, receptor_targets,
)
from amdockvs.docking.shard_tasks import by_fragment, dock_stage, hit_poses, off_target_elements, rank
from amdockvs.molecules.storage import iter_prepared_shards
from amdockvs.screening.campaign import finish_run, plan_targets, stop_run

# What a chunk of `vs` says about one receptor, which an off-target says for itself.
_RECEPTOR_KEYS = ("receptor_id", "receptor_name", "receptor_path", "flex_receptor_path", "box_center", "box_size",
                  "spacing", "chunks_total", "shards")


class DockCampaignParams(DockShardsJobParams):
    """`DockShardsJobParams`, and the off-targets the one receptor of the scope is checked against."""

    off_receptor_set_id: int | None = Field(default=None, ge=1)
    off_receptor_filters: dict[str, Any] = Field(default_factory=dict)
    cpu_required: int = Field(default=1, ge=1)  # of one chunk: one shard against one receptor

    @property
    def has_off_targets(self) -> bool:
        return bool(self.off_receptor_set_id or self.off_receptor_filters)


class CampaignWriter(ShardHitWriter):
    """What comes back, here: receipts without a fragment path (the fragments stay there, so
    nothing here ranks again), and the hits, which come ranked."""

    def write(self, results: list) -> None:
        for result in results:
            if result["stage"] == "hits":
                self._materialize_selected(self._of_run(result["hits"]))
                continue
            envelopes = result["envelopes"]
            self._materialize_selected(self._of_run(hit for envelope in envelopes for hit in envelope["hits"]))
            self.handle("", [{**envelope, "hits": [], "result_shard": ""} for envelope in envelopes])

    def _of_run(self, hits) -> list[dict]:
        return [{**hit, "docking_run_id": self.docking_run_id} for hit in hits]

    def finish(self) -> None:
        # Submitted again under another run id, it is that run's now: moved, not copied.
        supersede_results(self.project_db, run_id=self.run_id)
        finish_run(self.project_db, docking_run_id=self.docking_run_id)


def _writer(params: DockCampaignParams, project) -> CampaignWriter:
    return CampaignWriter(project_db=configs[project]["project_db"], hit_cap=params.hit_cap,
                          hit_threshold=params.hit_threshold, run_id=params.run_id, engine=params.engine,
                          protocol_hash=str(params.protocol_metadata.hash or ""))


def _ended(params: DockCampaignParams, project, snapshot) -> None:
    """Canceled or failed, its receptors say so. Interrupted (this side closed), it goes on there."""
    if snapshot.status.startswith("completed") or snapshot.status == "interrupted":
        return
    project_db = configs[project]["project_db"]
    stop_run(project_db, docking_run_id=find_docking_run(
        project_db, run_id=params.run_id, engine=params.engine, run_kind="screening",
        protocol_hash=str(params.protocol_metadata.hash or "")),
        state=TargetState.CANCELED if snapshot.status == "canceled" else TargetState.FAILED)


def _there(chunk: dict, cpu: int) -> dict:
    """A chunk of `dock_shards_job` as an element of `vs`: its receptor files travel, and it says
    nothing of the run, which is not the campaign's identity (the writer knows it)."""
    return {**chunk, "stage": "vs", "run_id": "", "docking_run_id": None, "_cpu_required": cpu,
            "receptor_path": worker_file(chunk["receptor_path"]),
            **({"flex_receptor_path": worker_file(chunk["flex_receptor_path"])} if chunk.get("flex_receptor_path") else {})}


@campaign(name="amdock_dock_shards_campaign", params_model=DockCampaignParams, output=_writer,
          volatile=("run_id",), finalize=_ended)
def dock_shards_campaign(params: DockCampaignParams, project) -> list[Stage]:
    config = configs[project]
    project_db = config["project_db"]
    # The whole scope every time: what is already there is found by the campaign, not left out here.
    values = {**params.model_dump(mode="python"), "skip_existing": False}
    chunks = [chunk for chunk in DockShardsJobSpec.build_chunks(values, config) if chunk.get("shards")]
    if not chunks:
        raise RuntimeError("Nothing to dock: no prepared shard against a prepared receptor with a binding site.")
    run = chunks[0]["docking_run_id"]
    elements = [_there(chunk, params.cpu_required) for chunk in chunks]
    digests = {worker_file(row["prepared_path"]): row["sha256"]
               for row in iter_prepared_shards(project_db, engine=params.preparation_engine) if row["sha256"]}
    stages = [Stage("vs", dock_stage, elements, reduce=partial(rank, cap=params.hit_cap, threshold=params.hit_threshold),
                    digests=digests),
              Stage("hits", hit_poses, From("vs", by_fragment))]
    if not params.has_off_targets:
        return stages
    references = {int(element["receptor_id"]) for element in elements}
    if len(references) != 1:
        raise ValueError("Off-targets are checked against one reference receptor; the scope has "
                         f"{len(references)}.")
    [reference] = references
    off = DockShardsJobParams(**{**values, "receptor_set_id": params.off_receptor_set_id,
                                 "receptor_filters": params.off_receptor_filters})
    output_dir = resolve_docking_output_dir(values, config) / SCREENING_SUBDIR
    targets = [target for target in receptor_targets(project_db, off, output_dir)
               if int(target["receptor_id"]) != reference]
    if not targets:
        raise RuntimeError("No off-target is a prepared receptor with a binding site.")
    top = int(params.selected_top_n or params.hit_cap)
    plan_targets(project_db, docking_run_id=run, receptors=targets, ligands_total=top, engine=params.engine)
    targets = [{key: worker_file(value) if key in ("receptor_path", "flex_receptor_path") else value
                for key, value in target.items() if value or key != "flex_receptor_path"} for target in targets]
    template = {key: value for key, value in elements[0].items() if key not in _RECEPTOR_KEYS}
    shards = {shard["shard_id"]: shard for element in elements for shard in element["shards"]}
    stages.append(Stage("off", dock_stage, From("vs", off_target_elements, (template, targets, shards, reference, top))))
    return stages


__all__ = ["CampaignWriter", "DockCampaignParams", "dock_shards_campaign"]
