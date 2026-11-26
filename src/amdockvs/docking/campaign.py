"""Docking on a cluster: the whole screening is one job of its queue.

A sharded library (`dock_shards_campaign`):

    prep  the shards not prepared here, prepared there and left there (only when there are some)
    vs    every prepared shard against the receptors, its fragments left there; ranked there
    hits  the poses of the best `hit_cap` of each receptor, read from those fragments
    off   with off-targets: the reference receptor's best against each of them

What comes back is what `vs` docked (the progress), the hits and the off-target hits: what
`dock_shards_job` would have written here, without a fragment travelling. The campaign is an
ms_flow job (`ms_flow.campaign`): submitted again, with another run id too, it is found where it
is by its identity, and what comes again is written once — done-sets and hits are upserts, the
counters are counted again, not added to, and under a new run id the hits move to that run.

Ligand rows (`dock_rows_campaign`): the chunks `docking_job` would run here, their ligands packed
in transport shards (`pack_rows`); what comes back is what that job writes, to the same tables.
"""
from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from pydantic import BaseModel, Field
from ms_flow.campaign import From, Stage, campaign
from ms_flow.core.data import catalog
from ms_flow.core.data.catalog import active_generation_id
from ms_flow.core.data.shard import MAX_RECORDS
from ms_flow.engine import File
from ms_flow.engine.files import KeptDir
from ms_flow.runner import plain, unplain

from amdockvs.core.jobs import JobSpec, configs
from amdockvs.core.vocab import TargetState
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.docking.jobs import DockingJobParams, DockingJobSpec, _supersede
from amdockvs.docking.repository import project_db_path, resolve_docking_output_dir
from amdockvs.docking.results.repository import find_docking_run
from amdockvs.docking.rerun import supersede_shard_runs
from amdockvs.docking.shard_jobs import (
    SCREENING_SUBDIR, DockShardsJobParams, DockShardsJobSpec, ShardHitWriter, receptor_targets,
)
from amdockvs.docking.tasks import dock_rows, pack_transport
from amdockvs.docking.shard_tasks import (
    by_fragment, dock_stage, hit_poses, off_target_elements, prepare_stage, prepared_elements, rank,
)
from amdockvs.molecules.storage import prepared_dir, prepared_role
from amdockvs.screening.campaign import finish_run, plan_targets, reopen_run, stop_run

# What a chunk of `vs` says about one receptor, which an off-target says for itself.
_RECEPTOR_KEYS = ("receptor_id", "receptor_name", "receptor_path", "flex_receptor_path", "box_center", "box_size",
                  "spacing", "input_key", "chunks_total", "shards")


class DockCampaignParams(DockShardsJobParams):
    """`DockShardsJobParams`, and the off-targets the one receptor of the scope is checked against."""

    off_receptor_set_id: int | None = Field(default=None, ge=1)
    off_receptor_filters: dict[str, Any] = Field(default_factory=dict)
    cpu_required: int = Field(default=1, ge=1)  # of one chunk: one shard against one receptor
    # The queue it goes to, as this project names it: where the files it prepares there stay.
    # Not its identity — the same campaign is the same campaign, whichever queue it is sent to.
    executor: str = ""

    @property
    def has_off_targets(self) -> bool:
        return bool(self.off_receptor_set_id or self.off_receptor_filters)


class CampaignWriter(ShardHitWriter):
    """What comes back, here: the shards marked done without a result file (the files stay
    there, so nothing here ranks again), and the hits, which come ranked."""

    def __init__(self, *, executor: str = "", preparation_engine: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.executor, self.preparation_engine = str(executor), str(preparation_engine)

    def write(self, results: list) -> None:
        for result in results:
            if result["stage"] == "prep":
                self._prepared_there(result)
                continue
            if result["stage"] == "hits":
                self._materialize_selected(self._of_run(result["hits"]))
                continue
            envelopes = result["envelopes"]
            self._materialize_selected(self._of_run(hit for envelope in envelopes for hit in envelope["hits"]))
            self.handle("", [{**envelope, "hits": [], "result_shard": ""} for envelope in envelopes])

    def _prepared_there(self, result: dict) -> None:
        """A shard prepared on the queue's own disk: its file is registered as being there, so the
        Shards view says where it is and only a step running there opens it. Registered once: a
        campaign that found it already made returns the same row."""
        if not self.executor:
            return
        generation_id = active_generation_id(self.project_db)
        for row in result.get("files") or []:
            catalog.add_remote(self.project_db, row["path"], location=self.executor, role=row["role"],
                               counts=row, generation_id=generation_id)

    def _of_run(self, hits) -> list[dict]:
        return [{**hit, "docking_run_id": self.docking_run_id} for hit in hits]

    def finish(self) -> None:
        # Submitted again under another run id, it is that run's now: moved, not copied —
        # with whatever the pairs it docked again had there.
        supersede_shard_runs(self.project_db, docking_run_id=self.docking_run_id)
        finish_run(self.project_db, docking_run_id=self.docking_run_id)


def _writer(params: DockCampaignParams, project) -> CampaignWriter:
    return CampaignWriter(project_db=configs[project]["project_db"], hit_cap=params.hit_cap,
                          hit_threshold=params.hit_threshold, run_id=params.run_id, engine=params.engine,
                          protocol_hash=str(params.protocol_metadata.hash or ""), executor=params.executor,
                          preparation_engine=params.preparation_engine)


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
    finds it there; its chunks in `elements` are told so (an element of `vs` waits for them).
    One the host already holds is not prepared again — that `prepare_stage` sees, not this side,
    so the campaign is the same campaign however much of it that host has done."""
    prep = {}
    for element in elements:
        for place, shard in enumerate(element["shards"]):
            if shard["shard_path"]:
                continue
            # There, a file of this one shard (whole: no `prepared_shard`), named for `located`.
            # Kept: in the project's folder there, so the next campaign to that host docks it as is.
            where = {"prepared_in": KeptDir(worker_output_dir(prepared_dir(shard["molecules"], engine))),
                     "prepared_as": f"{shard['shard_id']}{Path(shard['molecules']).suffix}"}
            element["shards"][place] = {**{k: v for k, v in shard.items() if k not in ("molecules", "shard")}, **where}
            prep.setdefault(shard["shard_id"], {"engine": engine, "role": prepared_role(engine), "shards": [{
                "shard_id": shard["shard_id"], "shard_path": shard["molecules"], "shard": shard.get("shard"),
                "import_batch_id": shard.get("import_batch_id"), "output_dir": where["prepared_in"],
                "output_name": where["prepared_as"]}]})
    return list(prep.values())


@campaign(name="amdock_dock_shards_campaign", params_model=DockCampaignParams, output=_writer,
          volatile=("run_id", "executor"), finalize=_ended)
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
    ranked = partial(rank, cap=params.hit_cap, threshold=params.hit_threshold)
    prep = _unprepared(elements, params.preparation_engine)
    stages = [Stage("prep", prepare_stage, prep),
              Stage("vs", dock_stage, From("prep", prepared_elements, (elements,)), reduce=ranked)] if prep else [
              Stage("vs", dock_stage, elements, reduce=ranked)]
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


def _plan(chunks: Iterable[dict], cpu: int, elements: list[dict]) -> Iterator[list[tuple[int, bytes]]]:
    """The chunks of `docking_job` as the elements of `dock_rows`, appended to `elements`: their
    ligands, once each, planned into transport shards in the order they come (yielded as each
    closes: a new one when an id can not follow the last, the ids of a shard only increase within
    `MAX_RECORDS` of its first, or it holds `TRANSPORT_RECORDS`), and what the pairs of a chunk
    share (its receptor, its box) said once. An element's `ligands` are indexes of those shards."""
    where: dict[int, int] = {}
    records: list[tuple[int, bytes]] = []
    index = 0
    for chunk in chunks:
        pairs = [dict(pair) for pair in chunk["pairs"]]
        mine = set()
        for pair in pairs:
            if "ligand" not in pair:
                continue
            ligand_id, data = int(pair["ligand_id"]), bytes(pair.pop("ligand"))
            if ligand_id not in where:
                if records and (ligand_id <= records[-1][0] or ligand_id - records[0][0] >= MAX_RECORDS
                                or len(records) >= TRANSPORT_RECORDS):
                    yield records
                    records, index = [], index + 1
                records.append((ligand_id, data))
                where[ligand_id] = index
            mine.add(where[ligand_id])
        common = {key: value for key, value in pairs[0].items()
                  if all(key in pair and pair[key] == value for pair in pairs[1:])}
        elements.append({**chunk, "_cpu_required": cpu, "ligands": sorted(mine), "common": common,
                         "pairs": [{key: value for key, value in pair.items() if key not in common} for pair in pairs]})
    if records:
        yield records


def _write_manifest(folder: Path, elements: list[dict], shards: list[tuple[str, str]]) -> Path:
    """The manifest of `elements`, their shard indexes now the files `shards` ([(path, sha)], in
    index order), named by its sha256: the same ligands, the same name."""
    for element in elements:
        element["ligands"] = [File(str(shards[index][0])) for index in element["ligands"]]
    text = json.dumps({"elements": plain(elements, "a docking chunk"),
                       "digests": {str(path): sha for path, sha in shards}}, sort_keys=True)
    manifest = folder / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def pack_rows(chunks: Iterable[dict], folder: Path, cpu: int) -> Path:
    """The chunks of `docking_job` as the elements of `dock_rows`, written to a manifest under
    `folder` (returned), their ligands moved into transport shards there. In process: what
    `PackRowsJobSpec` does with workers, the same files and the same manifest.

    Read by the campaign each time it is submitted, so it is the same campaign however many of
    its pairs came back since. Nothing registers the shards: they are not a library, and the
    project stays one of rows (`ms_shard_files` is what makes it one of shards).
    Removed when the campaign completes (`drop_transport`); a stopped one keeps them for Resume.
    """
    folder.mkdir(parents=True, exist_ok=True)
    elements: list[dict] = []
    shards = [pack_transport({"folder": str(folder), "index": index, "records": records})
              for index, records in enumerate(_plan(chunks, cpu, elements))]
    return _write_manifest(folder, elements, [(shard["path"], shard["sha"]) for shard in shards])


class PackRowsParams(BaseModel):
    """A run over rows to pack for a queue: its `docking_job` params, where, and `key`, what
    names the job's own files there until the manifest is written (`packed_manifest`)."""

    docking: dict[str, Any] = Field(default_factory=dict)
    folder: str
    cpu: int = 1
    key: str


def pack_params(docking: dict, folder: Path, *, cpu: int) -> dict:
    return PackRowsParams(docking=dict(docking), folder=str(folder), cpu=int(cpu), key=uuid4().hex).model_dump()


def _job_files(params: PackRowsParams) -> tuple[Path, Path]:
    """(the planned elements, the names of the shards written): the job's, until the manifest."""
    folder = Path(params.folder)
    return folder / f".plan-{params.key}.json", folder / f".plan-{params.key}.names"


class PackNames:
    """Parent-side sink of `PackRowsJobSpec`: the name of each shard a worker wrote, one line each."""

    def __init__(self, params: PackRowsParams) -> None:
        self.path = _job_files(params)[1]

    def handle(self, chunk_id: str, result: Any) -> None:
        if result:
            with open(self.path, "a", encoding="utf-8") as file:
                file.write(json.dumps([int(result["index"]), str(result["path"]), str(result["sha"])]) + "\n")

    def flush(self) -> None:
        """Every chunk wrote what it brought."""

    def on_job_terminal(self, status: str) -> None:
        """What is left for the manifest is the caller's (`packed_manifest`)."""

    def close(self) -> None:
        """Nothing held open."""


def pack_names(params: dict, config: dict) -> PackNames:
    return PackNames(PackRowsParams(**params))


class PackRowsJobSpec(JobSpec):
    """The ligands of a run over rows into transport shards: the parent plans (one chunk per
    shard, its ligands' bytes as the database holds them), a worker writes and names the file,
    the parent keeps the names. The manifest is written after it (`packed_manifest`)."""

    name = "amdock_pack_rows_job"
    task_name = "amdock_pack_rows"
    description = "Pack the ligands of a docking run over rows for a cluster's queue."
    params_model = PackRowsParams
    executor = "compute"
    result_handler_factory = pack_names
    store_results = False
    required = ()
    produces = ()
    run_chunk = staticmethod(pack_transport)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = PackRowsParams(**params)
        plan, names = _job_files(parsed)
        plan.parent.mkdir(parents=True, exist_ok=True)
        names.unlink(missing_ok=True)
        elements: list[dict] = []
        emitted = False
        for index, records in enumerate(_plan(DockingJobSpec.build_chunks(parsed.docking, config), parsed.cpu,
                                              elements)):
            emitted = True
            yield {"folder": parsed.folder, "index": index, "records": records}
        plan.write_text(json.dumps(plain(elements, "a docking chunk")), encoding="utf-8")
        if not emitted:  # MF needs one chunk to close the job out
            yield {"records": []}


pack_rows_job = PackRowsJobSpec.to_job_definition()


def packed_manifest(params: dict) -> Path:
    """The manifest of a finished `PackRowsJobSpec`, written from its plan and the names of what
    its workers wrote; the job's own files go."""
    parsed = PackRowsParams(**params)
    plan, names = _job_files(parsed)
    if not plan.exists():
        raise RuntimeError(f"Packing {parsed.key} left no plan: it did not finish.")
    elements = unplain(json.loads(plan.read_text(encoding="utf-8")))
    lines = names.read_text(encoding="utf-8").splitlines() if names.exists() else []
    shards = [(path, sha) for _index, path, sha in sorted(json.loads(line) for line in lines)]
    manifest = _write_manifest(Path(parsed.folder), elements, shards)
    plan.unlink(missing_ok=True)
    names.unlink(missing_ok=True)
    return manifest


def transport_folder(project_db) -> Path:
    return project_db_path(project_db).parent / "cache" / "transport"


def drop_transport(manifest: Path) -> None:
    """A manifest and the transport shards no other manifest beside it names: what a campaign of
    rows leaves once it ended. Shards are named by content, so two campaigns may share one.

    ponytail: a campaign being packed while another ends has no manifest yet, so a shard both
    would share can go from under it; its submit then fails on the missing file, never docks wrong.
    """
    def named(path: Path) -> set[str]:
        try:
            return set(json.loads(path.read_text(encoding="utf-8"))["digests"])
        except (OSError, ValueError, KeyError):
            return set()

    from ms_flow.core.data.companions import whole

    others = set().union(*(named(path) for path in manifest.parent.glob("*.json") if path != manifest))
    for shard in named(manifest) - others:
        for path in whole(shard):  # its index goes with it
            Path(path).unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)


def clear_transport(project_db) -> int:
    """Every file campaigns of rows packed for a queue, by hand (Project > Clear queue files): the
    ones canceled or failed lose their Resume. Returns the bytes freed."""
    folder = transport_folder(project_db)
    files = [path for path in folder.glob("*") if path.is_file()] if folder.is_dir() else []
    freed = sum(path.stat().st_size for path in files)
    for path in files:
        path.unlink(missing_ok=True)
    return freed


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
    # Canceled or failed, Resume submits it again from the same manifest: it stays until it completes.
    # One never resumed leaves its files in cache/transport until they are cleared (`clear_transport`).
    if status == "completed":
        drop_transport(Path(params.elements))


@campaign(name="amdock_dock_rows_campaign", params_model=DockRowsCampaignParams,
          output=DockingJobSpec.output_spec, finalize=_rows_ended)
def dock_rows_campaign(params: DockRowsCampaignParams, project) -> list[Stage]:
    try:
        manifest = json.loads(Path(params.elements).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError("Its ligands were packed for the queue and those files are gone (cleared, or "
                           "it ended before they were kept for Resume). Dock it again: what is docked is skipped.") from None
    project_db = configs[project]["project_db"]
    reopen_run(project_db, docking_run_id=find_docking_run(
        project_db, run_id=params.run_id, engine=params.engine, run_kind="screening",
        protocol_hash=str(params.protocol_metadata.hash or "")))
    return [Stage("vs", dock_rows, unplain(manifest["elements"]), digests=manifest["digests"])]


__all__ = ["CampaignWriter", "clear_transport", "DockCampaignParams", "DockRowsCampaignParams", "dock_rows_campaign",
           "dock_shards_campaign", "drop_transport", "pack_params", "pack_rows", "pack_rows_job",
           "packed_manifest", "PackRowsJobSpec", "PackRowsParams", "transport_folder"]
