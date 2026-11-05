"""Docking on a cluster: the whole screening is one job of its queue.

A sharded library (`dock_shards_campaign`):

    prep  the shards not prepared here, prepared there and left there (only when there are some)
    vs    every prepared shard against the receptors, its fragments left there; ranked there
    hits  the poses of the best `hit_cap` of each receptor, read from those fragments
    off   with off-targets: the reference receptor's best against each of them

What comes back is the receipts of `vs` (the progress), the hits and the off-target hits: what
`dock_shards_job` would have written here, without a fragment travelling. The campaign is an
ms_flow job (`ms_flow.campaign`): submitted again, with another run id too, it is found where it
is by its identity, and what comes again is written once — receipts and hits are upserts, the
counters are counted again, not added to, and under a new run id the hits move to that run.

Ligand rows (`dock_rows_campaign`): the chunks `docking_job` would run here, their ligands packed
in transport shards (`pack_rows`); what comes back is what that job writes, to the same tables.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import partial
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from pydantic import Field
from ms_flow.campaign import From, Stage, campaign
from ms_flow.core.data.shard import MAX_RECORDS, PayloadKind, Serializer, ShardWriter
from ms_flow.engine import File
from ms_flow.runner import plain, unplain

from amdockvs.core.jobs import configs
from amdockvs.core.vocab import TargetState
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.docking.jobs import DockingJobParams, DockingJobSpec, _supersede
from amdockvs.docking.repository import project_db_path, resolve_docking_output_dir
from amdockvs.docking.results.repository import find_docking_run, supersede_results
from amdockvs.docking.shard_jobs import (
    SCREENING_SUBDIR, DockShardsJobParams, DockShardsJobSpec, ShardHitWriter, receptor_targets,
)
from amdockvs.docking.tasks import dock_rows
from amdockvs.docking.shard_tasks import (
    by_fragment, dock_stage, hit_poses, off_target_elements, prepare_stage, prepared_elements, rank,
)
from amdockvs.io.shards import SHARD_SUFFIX
from amdockvs.molecules.storage import iter_prepared_shards, prepared_shard_path
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


def _unprepared(elements: list[dict], engine: str) -> list[dict]:
    """The `prep` elements of a campaign: a shard not prepared here, once, and where `located`
    finds it there; its chunks in `elements` are told so (an element of `vs` waits for them)."""
    prep = {}
    for element in elements:
        for place, shard in enumerate(element["shards"]):
            if shard["shard_path"]:
                continue
            twin = prepared_shard_path(shard["molecules"], engine)
            where = {"prepared_in": worker_output_dir(twin.parent), "prepared_as": twin.name}
            element["shards"][place] = {**{k: v for k, v in shard.items() if k != "molecules"}, **where}
            prep.setdefault(shard["shard_id"], {"engine": engine, "shards": [{
                "shard_id": shard["shard_id"], "shard_path": shard["molecules"], "output_dir": where["prepared_in"]}]})
    return list(prep.values())


@campaign(name="amdock_dock_shards_campaign", params_model=DockCampaignParams, output=_writer,
          volatile=("run_id",), finalize=_ended)
def dock_shards_campaign(params: DockCampaignParams, project) -> list[Stage]:
    config = configs[project]
    project_db = config["project_db"]
    # The whole scope every time: what is already there is found by the campaign, not left out here.
    values = {**params.model_dump(mode="python"), "skip_existing": False}
    chunks = [chunk for chunk in DockShardsJobSpec.build_chunks(values, {**config, "unprepared": True})
              if chunk.get("shards")]
    if not chunks:
        raise RuntimeError("Nothing to dock: no shard against a prepared receptor with a binding site.")
    run = chunks[0]["docking_run_id"]
    elements = [_there(chunk, params.cpu_required) for chunk in chunks]
    digests = {worker_file(row["prepared_path"]): row["sha256"]
               for row in iter_prepared_shards(project_db, engine=params.preparation_engine) if row["sha256"]}
    ranked = partial(rank, cap=params.hit_cap, threshold=params.hit_threshold)
    prep = _unprepared(elements, params.preparation_engine)
    stages = [Stage("prep", prepare_stage, prep, deliver="none"),
              Stage("vs", dock_stage, From("prep", prepared_elements, (elements,)), reduce=ranked)] if prep else [
              Stage("vs", dock_stage, elements, reduce=ranked, digests=digests)]
    stages.append(Stage("hits", hit_poses, From("vs", by_fragment)))
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



# --- Ligand rows ---------------------------------------------------------------------------

TRANSPORT_RECORDS = 1000  # ligands of a transport shard: how many files travel, not how many tasks run


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class _Packer:
    """Ligands, once each, into transport shards under `folder` in the order they come; a shard
    is named by its sha256, so the same ligands make the same file."""

    def __init__(self, folder: Path):
        self.folder, self.where, self.shards = folder, {}, []
        self._writer, self._last, self._count = None, -1, 0

    def put(self, ligand_id: int, data: bytes) -> int:
        """The index of the shard `ligand_id` is in: a new one when it can not follow the last
        (the ids of a shard only increase, within `MAX_RECORDS` of its first)."""
        if ligand_id in self.where:
            return self.where[ligand_id]
        writer = self._writer
        if (writer is None or ligand_id <= self._last or ligand_id - writer.base_id >= MAX_RECORDS
                or self._count >= TRANSPORT_RECORDS):
            self._close()
            self._writer = ShardWriter(self.folder / f".packing-{uuid4().hex}{SHARD_SUFFIX}", dataset_id=None,
                                       shard_id=0, base_id=ligand_id, kind=PayloadKind.GENERIC_BYTES,
                                       serializer=Serializer.RAW, slot_count=MAX_RECORDS)
            self._count = 0
        self._writer.add(ligand_id, data)
        self._last, self._count = ligand_id, self._count + 1
        self.where[ligand_id] = len(self.shards)
        return len(self.shards)

    def _close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        sha = _sha256(self._writer.path)
        path = self.folder / f"{sha}{SHARD_SUFFIX}"
        os.replace(self._writer.path, path)
        self.shards.append((path, sha))
        self._writer = None

    def close(self) -> list[tuple[Path, str]]:
        self._close()
        return self.shards


def pack_rows(chunks: Iterable[dict], folder: Path, cpu: int) -> Path:
    """The chunks of `docking_job` as the elements of `dock_rows`, written to a manifest under
    `folder` (returned), named by its sha256: their ligands moved into transport shards there,
    and what the pairs of a chunk share (its receptor, its box) said once.

    Read by the campaign each time it is submitted, so it is the same campaign however many of
    its pairs came back since. Nothing registers the shards: they are not a library, and the
    project stays one of rows (`screening_shards` is what makes it one of shards).
    ponytail: the files stay in the cache; removed with it (C6 cleanup).
    """
    folder.mkdir(parents=True, exist_ok=True)
    packer, elements = _Packer(folder), []
    for chunk in chunks:
        pairs = [dict(pair) for pair in chunk["pairs"]]
        shards = sorted({packer.put(int(pair["ligand_id"]), bytes(pair.pop("ligand"))) for pair in pairs
                         if "ligand" in pair})
        common = {key: value for key, value in pairs[0].items()
                  if all(key in pair and pair[key] == value for pair in pairs[1:])}
        elements.append({**chunk, "_cpu_required": cpu, "ligands": shards, "common": common,
                         "pairs": [{key: value for key, value in pair.items() if key not in common} for pair in pairs]})
    shards = packer.close()
    for element in elements:
        element["ligands"] = [File(str(shards[index][0])) for index in element["ligands"]]
    text = json.dumps({"elements": plain(elements, "a docking chunk"),
                       "digests": {str(path): sha for path, sha in shards}}, sort_keys=True)
    manifest = folder / f"{hashlib.sha256(text.encode()).hexdigest()}.json"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def transport_folder(project_db) -> Path:
    return project_db_path(project_db).parent / "cache" / "transport"


class DockRowsCampaignParams(DockingJobParams):
    """`DockingJobParams`, and the manifest its chunks became when it was submitted (`pack_rows`)."""

    elements: str


def _rows_ended(params: DockRowsCampaignParams, project, snapshot) -> None:
    """As `docking_job` ends (`_supersede`). Interrupted (this side closed), it goes on there."""
    if snapshot.status == "interrupted":
        return
    status = ("completed" if snapshot.status.startswith("completed")
              else TargetState.CANCELED if snapshot.status == "canceled" else TargetState.FAILED)
    _supersede(params.model_dump(mode="python"),
               {"project_db": configs[project]["project_db"], "terminal_status": status})


@campaign(name="amdock_dock_rows_campaign", params_model=DockRowsCampaignParams,
          output=DockingJobSpec.output_spec, finalize=_rows_ended)
def dock_rows_campaign(params: DockRowsCampaignParams, project) -> list[Stage]:
    manifest = json.loads(Path(params.elements).read_text(encoding="utf-8"))
    return [Stage("vs", dock_rows, unplain(manifest["elements"]), digests=manifest["digests"])]


__all__ = ["CampaignWriter", "DockCampaignParams", "DockRowsCampaignParams", "dock_rows_campaign",
           "dock_shards_campaign", "pack_rows", "transport_folder"]
