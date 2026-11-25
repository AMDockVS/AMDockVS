"""Library records into molecule rows, as a job. The parent names the records (refs, one chunk
per shard); a worker opens the shard, parses and compresses; the parent only inserts.

A promotion is idempotent on (batch, record id): done again it finds its rows.
"""
from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel, Field
from sqlmodel import select

from ms_flow.core.data.catalog import shard_spec

from amdockvs.chemistry.shards import promotion_records
from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.core.jobs import JobSpec
from amdockvs.core.worker_io import worker_file
from amdockvs.io.batches import library_names
from amdockvs.io.shards import shard_key, shard_number, split_shard_key
from amdockvs.models import ClusteringResult, MoleculeRecord
from amdockvs.molecules.storage import ShardStore
from amdockvs.screening.materialize import promote_records


class PromoteJobParams(BaseModel):
    """`keys` (`io.shards.shard_key`), or with none, the representatives a clustering run's
    sidecar flags. With `cluster_run_id`, a promoted record not in the run yet becomes its row."""

    keys: list[int] = Field(default_factory=list)
    cluster_run_id: str = ""
    method: str = ""
    fp_type: str = ""


def _sidecar(project_db, run_id: str) -> Path:
    return Path(project_db.db_path).expanduser().resolve().parent / "clustering_runs" / f"{run_id}.parquet"


def _wanted(project_db, params: PromoteJobParams) -> dict[int, dict[int, int | None]]:
    """{batch: {record id: cluster id}}."""
    pairs: Iterable[tuple[int, int | None]] = ((int(key), None) for key in params.keys)
    if not params.keys and params.cluster_run_id:
        import pyarrow.parquet as pq

        # A run over shards keeps its members by reference: no sidecar, no representatives.
        table = pq.read_table(_sidecar(project_db, params.cluster_run_id),
                              columns=["molecule_id", "cluster_id", "is_centroid"]).to_pydict()
        pairs = ((int(key), int(cluster)) for key, cluster, centroid
                 in zip(table["molecule_id"], table["cluster_id"], table["is_centroid"]) if centroid)
    wanted: dict[int, dict[int, int | None]] = {}
    for key, cluster in pairs:
        batch, record_id = split_shard_key(key)
        wanted.setdefault(int(batch), {})[int(record_id)] = cluster
    return wanted


def promoted_ids(project_db, keys: Iterable[int]) -> dict[int, int]:
    """{key: molecule id} of the keys already promoted."""
    wanted: dict[int, set[int]] = {}
    for key in keys:
        batch, record_id = split_shard_key(int(key))
        wanted.setdefault(int(batch), set()).add(int(record_id))
    found: dict[int, int] = {}
    with project_db.get_session() as session:
        for batch, record_ids in wanted.items():
            ordered = sorted(record_ids)
            for start in range(0, len(ordered), 900):
                for molecule_id, index in session.exec(
                    select(MoleculeRecord.id, MoleculeRecord.source_index).where(
                        MoleculeRecord.is_ligand.is_(True), MoleculeRecord.import_batch_id == batch,
                        MoleculeRecord.source_index.in_(ordered[start:start + 900]))
                ).all():
                    found[shard_key(batch, int(index))] = int(molecule_id)
    return found


class PromotionWriter:
    """Parent-side sink: the records a worker brought become rows (once each), and with a run,
    the run's rows for the ones it lacks."""

    def __init__(self, *, project_db: Any, params: PromoteJobParams) -> None:
        self.project_db, self.params = project_db, params

    def handle(self, chunk_id: str, result: Any) -> None:
        records = list((result or {}).get("records") or [])
        promoted = promote_records(self.project_db, records)
        run_id = self.params.cluster_run_id
        if not promoted or not run_id:
            return
        cluster_of = {(r.get("import_batch_id"), int(r["source_index"])): r.get("cluster_id") for r in records}
        of_run = ClusteringResult.cluster_run_id == run_id
        with self.project_db.get_session() as session:
            known = set(session.exec(select(ClusteringResult.molecule_id).where(
                of_run, ClusteringResult.molecule_id.in_(list(promoted.values())))).all())
            session.add_all(
                ClusteringResult(molecule_id=molecule_id, cluster_run_id=run_id, cluster_id=int(cluster_of[key]),
                                 is_centroid=True, method=self.params.method, fp_type=self.params.fp_type)
                for key, molecule_id in promoted.items() if molecule_id not in known and cluster_of[key] is not None
            )
            session.commit()

    def flush(self) -> None:
        """Every chunk wrote what it brought."""

    def on_job_terminal(self, status: str) -> None:
        """Nothing to finish: what committed stays, a redo finds it."""

    def close(self) -> None:
        """Nothing held open."""


def promotion_writer(params: dict, config: dict) -> PromotionWriter:
    return PromotionWriter(project_db=config["project_db"], params=PromoteJobParams(**params))


class PromoteJobSpec(JobSpec):
    """Library records into molecule rows: one chunk per shard that holds any of them."""

    name = "amdock_promote_records_job"
    task_name = "amdock_promote_records"
    description = "Turn library records into molecule rows."
    params_model = PromoteJobParams
    executor = "compute"
    result_handler_factory = promotion_writer
    store_results = False
    required = ()
    produces = ()
    run_chunk = staticmethod(promotion_records)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = PromoteJobParams(**params)
        project_db = dict(config or {})["project_db"]
        wanted = _wanted(project_db, parsed)
        libraries = library_names(project_db) if wanted else {}
        emitted = False
        for batch, clusters in wanted.items():
            ordered = sorted(clusters)
            # asked by key: excluded or not
            for row in ShardStore(project_db).iter_rows(shard_spec(scope=batch), batch_size=1000, whole=True):
                lo = bisect_left(ordered, int(row["base_id"]))
                hi = bisect_left(ordered, int(row["base_id"]) + int(row["slot_count"]))
                if lo == hi:
                    continue
                mine = ordered[lo:hi]
                emitted = True
                yield {"shard_path": worker_file(row["path"]), "shard": shard_number(row), "import_batch_id": batch,
                       "library": libraries.get(batch, "library"), "record_ids": mine,
                       "cluster_ids": [clusters[i] for i in mine]}
        if not emitted:  # MF needs one chunk to close the job out
            yield {"record_ids": []}


promote_records_job = PromoteJobSpec.to_job_definition()


def promotion_params(keys: Iterable[int] = (), **params: Any) -> dict:
    return PromoteJobParams(keys=[int(key) for key in keys], **params).model_dump(mode="python")


def promote(runtime, keys: Iterable[int], *, cluster_run_id: str = "", **params: Any) -> dict[int, int]:
    """{key: molecule id}: submits the promotion and waits for it. Off the GUI thread only.
    With a run, the records are the representatives its sidecar flags (`keys` reads them back)."""
    keys = [int(key) for key in keys]
    if not keys:
        return {}
    job_id = runtime.submit_job(
        promote_records_job, params=promotion_params(() if cluster_run_id else keys, cluster_run_id=cluster_run_id,
                                                     **params),
        executor_name=DEFAULT_LOCAL_CPU_EXECUTOR,
    )
    status = runtime.wait_for_job(job_id, poll_s=0.1)
    if not str(status.status).lower().endswith("completed"):
        raise RuntimeError(f"Promotion job {job_id} ended {status.status}: {status.error or ''}")
    return promoted_ids(runtime.workspace.project_db, keys)


__all__ = ["PromoteJobParams", "PromoteJobSpec", "promote", "promote_records_job", "promoted_ids",
           "promotion_params"]
