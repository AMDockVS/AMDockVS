"""Diversity-selection job — cluster the library's fingerprints and persist assignments.

Async over the project DB via MolSuite: the job function reads fingerprints through the
MolSuite query API (``db_rows``) on the orchestrator side (where project_db lives), hands
one payload to a pure-compute task, and the task's rows land in the ``clustering_results`` table
through a MolSuite ``table_sink``. No worker-side DB connections — input and output both go
through MolSuite, so there is no direct project.db access from inside a task.
"""
from __future__ import annotations

import shutil
import uuid
from itertools import batched
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.engine import writer
from ms_flow.query import QuerySpec, db_rows
from ms_flow.sinks import table_sink
from amdockvs.core.jobs import configs, job
from amdockvs.diversity.tasks import cluster_molecules_task, shard_fingerprints_task

from amdockvs.core.configuration import DEFAULT_LIGAND_BATCH_SIZE
from amdockvs.molecules.storage import ShardStore, as_store, store_from_config
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.core.constants import (
    AMDOCKVS_OFFLOADABLE_EXECUTORS,
    TABLE_ACTIVITIES,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
from amdockvs.models import ClusteringResult
from amdockvs.models.descriptors import FingerprintType
from amdockvs.docking.repository import molecule_set_spec
from amdockvs.core.paths import set_default_project_root
from amdockvs.diversity.clustering import fp_matrix_from_bitstrings


def _fp_type_for_radius(radius: int) -> str:
    return FingerprintType.ECFP6 if int(radius) >= 3 else FingerprintType.ECFP4


class SelectionClusterJobParams(BaseModel):
    method: str = Field(default="bitbirch")
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    per_cluster: int = Field(default=1, ge=1)
    # An activity endpoint: the scope becomes the ligands that have a value for it.
    with_activity: str = Field(default="")
    molecule_set_id: int | None = Field(default=None, ge=1)
    molecule_filters: dict[str, Any] = Field(default_factory=dict)
    fp_radius: int = Field(default=2, ge=1)
    fp_nbits: int = Field(default=2048, ge=64)
    cluster_run_id: str = Field(default="")
    num_processes: int = Field(default=1, ge=1)  # >1 → bblean multiround (parallel); matches cpu_required
    # Resolved on the submit side, where a runtime (and so the project's settings layer) exists;
    # a worker only ever sees the number it was handed.
    batch_size: int = Field(default=DEFAULT_LIGAND_BATCH_SIZE, ge=1)
    # The library's shards instead of the table: the run reads the fingerprints
    # `shard_fingerprints_job` packed there, and only its representatives become rows.
    shards: bool = Field(default=False)


def _run_dir(project_db, run_id: str) -> Path:
    return Path(project_db.db_path).expanduser().resolve().parent / "clustering_runs" / run_id


def _scope_spec(params: SelectionClusterJobParams) -> QuerySpec:
    """The scope to cluster, resolved entirely in SQL. Active ligands by default.

    Set membership is a subquery, not a Python `set` holding every id in the set: that set
    grew with the library and also forced row-by-row filtering outside the db.
    """
    filters = {"is_ligand": True, "excluded": False, "stored_payload_id__is_not_null": True, **dict(params.molecule_filters or {})}
    filters.pop("_limit", None)
    in_specs: list[QuerySpec] = []
    if params.molecule_set_id is not None:
        in_specs.append(molecule_set_spec(int(params.molecule_set_id)))
    if params.with_activity:
        in_specs.append(QuerySpec(
            table=TABLE_ACTIVITIES, fields=("molecule_id",), filters={"activity_type": str(params.with_activity)}
        ))
    if in_specs:
        filters["id__in_subquery"] = in_specs
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "stored_payload_id", "current_payload_id"),
        filters=filters,
        order=("id",),
    )


def scope_molecule_rows(source, params: SelectionClusterJobParams) -> Iterator[dict[str, Any]]:
    """The molecules in the scope, in keyset pages. Flat rows: whoever wants to wrap them
    does so on the consumer side. `source` is the ligand store or the project db."""
    return as_store(source).iter_rows(_scope_spec(params), batch_size=int(params.batch_size))


def scope_molecule_count(source, params: SelectionClusterJobParams) -> int:
    """How many molecules the scope holds — a COUNT, not a walk over the whole library."""
    return as_store(source).count(_scope_spec(params))


def activity_values(project_db, endpoint: str) -> dict[int, float]:
    """The latest value of an activity endpoint per ligand. Measured ligands only, so it is
    small next to the library."""
    rows = db_rows(
        project_db, TABLE_ACTIVITIES, fields=("id", "molecule_id", "value"),
        filters={"activity_type": str(endpoint)}, order=("id",),
    )
    return {int(row["molecule_id"]): float(row["value"]) for row in rows}  # later rows win


def stored_fingerprints_for_ids(project_db, molecule_ids: list[int], *, radius: int, nbits: int) -> dict[int, bytes]:
    """Stored fingerprints for a bounded id list (chunked ``molecule_id__in`` — no full-table scan).
    Used by the interactive preview so it stays cheap even when the library has millions of rows."""
    ids = [int(i) for i in molecule_ids]
    out: dict[int, bytes] = {}
    for start in range(0, len(ids), 900):  # stay well under SQLite's bound-parameter limit
        chunk = ids[start:start + 900]
        rows = db_rows(
            project_db,
            TABLE_FINGERPRINTS,
            fields=("molecule_id", "fp_binary"),
            filters={
                "fp_type": _fp_type_for_radius(radius),
                "nbits": int(nbits),
                "radius": int(radius),
                "molecule_id__in": chunk,
            },
        )
        for row in rows:
            if row.get("fp_binary"):
                out[int(row["molecule_id"])] = row["fp_binary"]
    return out


def _fingerprint_from_structure(project_db, row: dict[str, Any], *, radius: int, nbits: int) -> bytes | None:
    """Compute a Morgan fingerprint bitstring for a molecule whose FingerprintRecord is missing.
    Fallback path so selection works without a prior fingerprint job (slower — serial, in-process)."""
    from amdockvs.chemistry.fingerprints import morgan_fingerprint
    from amdockvs.chemistry.structures import molecule_mol

    mol = molecule_mol(project_db, row)
    if mol is None:
        return None
    return morgan_fingerprint(mol, radius=int(radius), n_bits=int(nbits)).ToBitString().encode("ascii")


def _write_packed_fingerprints(
    project_db, params: SelectionClusterJobParams, *, out_dir: Path, store=None
) -> tuple[Path, Path, int]:
    """Dumps the scope to ``fingerprints.npy`` (bit-packed) + ``ids.npy``, in batches.

    The files are preallocated from the scope COUNT and filled through a memmap, so only one
    batch lives in RAM at a time: usage does not depend on library size. Returns
    ``(fp_path, ids_path, written)``; rows without a usable fingerprint are skipped, which is
    why the counter can end below the COUNT and the leftover tails are never read.
    """
    import numpy as np

    total = scope_molecule_count(store or project_db, params)
    out_dir.mkdir(parents=True, exist_ok=True)
    fp_path, ids_path = out_dir / "fingerprints.npy", out_dir / "ids.npy"
    if not total:
        return fp_path, ids_path, 0

    nbits = int(params.fp_nbits)
    packed = np.lib.format.open_memmap(fp_path, mode="w+", dtype=np.uint8, shape=(total, (nbits + 7) // 8))
    ids_out = np.lib.format.open_memmap(ids_path, mode="w+", dtype=np.int64, shape=(total,))
    written = 0
    try:
        for batch in batched(scope_molecule_rows(store or project_db, params), int(params.batch_size)):
            ids = [int(row["id"]) for row in batch]
            # bounded `molecule_id__in` per batch instead of a scan of the whole fingerprint table
            stored = stored_fingerprints_for_ids(project_db, ids, radius=params.fp_radius, nbits=nbits)
            keep_ids: list[int] = []
            bitstrings: list[bytes] = []
            for molecule_id, row in zip(ids, batch):
                fp = stored.get(molecule_id) or _fingerprint_from_structure(project_db, row, radius=params.fp_radius, nbits=nbits)
                if fp is None or len(fp) != nbits:
                    continue  # unparseable / no structure / wrong width -> excluded from clustering
                keep_ids.append(molecule_id)
                bitstrings.append(fp)
            if not keep_ids or written >= total:
                continue
            keep_ids = keep_ids[: total - written]
            block = fp_matrix_from_bitstrings(bitstrings[: len(keep_ids)])
            packed[written:written + len(keep_ids)] = np.packbits(block, axis=-1)
            ids_out[written:written + len(keep_ids)] = keep_ids
            written += len(keep_ids)
    finally:
        packed.flush()
        ids_out.flush()
        del packed, ids_out
    return fp_path, ids_path, written


def _concat_shard_fingerprints(src: Path, *, out_dir: Path, nbits: int) -> tuple[Path, Path, int]:
    """The per-shard parts into the one ``fingerprints.npy`` + ``ids.npy`` the task reads.

    ponytail: a copy (256 B/mol on disk, never in RAM: memmap to memmap); reading the parts in
    place would need a PackedFingerprints over many arrays.
    """
    import numpy as np

    parts = sorted(src.glob("*.ids.npy"))
    counts = [int(np.load(part, mmap_mode="r").shape[0]) for part in parts]
    total = sum(counts)
    fp_path, ids_path = out_dir / "fingerprints.npy", out_dir / "ids.npy"
    if total:
        packed = np.lib.format.open_memmap(fp_path, mode="w+", dtype=np.uint8, shape=(total, (nbits + 7) // 8))
        ids_out = np.lib.format.open_memmap(ids_path, mode="w+", dtype=np.int64, shape=(total,))
        at = 0
        for part, n in zip(parts, counts):
            packed[at:at + n] = np.load(str(part).replace(".ids.npy", ".fp.npy"), mmap_mode="r")
            ids_out[at:at + n] = np.load(part, mmap_mode="r")
            at += n
        packed.flush()
        ids_out.flush()
        del packed, ids_out
    shutil.rmtree(src, ignore_errors=True)
    return fp_path, ids_path, total


def _assignments(params, project):
    """The sink of the job, for chunks that return their rows beside the sidecar they wrote.
    A run over shards returns its representatives by `shard_key`: they are promoted first."""
    sink = writer(table_sink(model=ClusteringResult, write_mode="bulk"), project)
    shards = bool(dict(params).get("shards"))

    def write(results) -> None:
        batches = [result["rows"] for result in results]
        if shards:
            store = ShardStore(configs[project]["project_db"])
            promoted = [store.promote(row["molecule_id"] for row in rows) for rows in batches]
            batches = [[{**row, "molecule_id": ids[row["molecule_id"]]} for row in rows if row["molecule_id"] in ids]
                       for rows, ids in zip(batches, promoted)]
        sink.write(batches)

    return SimpleNamespace(tables=sink.tables, write=write)


def _drop_fingerprints(params: dict, config: dict) -> None:
    """The packed fingerprints of the run: the worker deletes the ones it read, which on another
    disk are copies."""
    run_id, project_db = str(dict(params).get("cluster_run_id") or ""), config.get("project_db")
    if run_id and getattr(project_db, "db_path", None):
        shutil.rmtree(_run_dir(project_db, run_id), ignore_errors=True)


def _drop_failed_fingerprints(params: dict, config: dict) -> None:
    """A finished fingerprint job hands its parts to the clustering; a failed one, to nobody."""
    if config.get("terminal_status") != "completed":
        _drop_fingerprints(params, config)


@job(
    task=shard_fingerprints_task,
    name="amdock_shard_fingerprints_job",
    params_model=SelectionClusterJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    finalize=_drop_failed_fingerprints,
)
def shard_fingerprints_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """One shard per chunk: the fingerprints a clustering over the library reads, packed beside
    its run. The clustering job waits for this one."""
    parsed = SelectionClusterJobParams(**params)
    project_db = dict(config or {}).get("project_db")
    if project_db is None:
        raise ValueError("shard_fingerprints_job requires project_db in config.")
    out_dir = _run_dir(project_db, parsed.cluster_run_id) / "shards"
    for row in ShardStore(project_db).iter_rows():
        yield {
            "shard_path": worker_file(row["path"]),
            "shard_id": int(row["id"]),
            "import_batch_id": row.get("import_batch_id"),
            "radius": int(parsed.fp_radius),
            "nbits": int(parsed.fp_nbits),
            "output_dir": worker_output_dir(out_dir),
        }


@job(
    task=cluster_molecules_task,
    name="amdock_cluster_molecules_job",
    params_model=SelectionClusterJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=_assignments,
    finalize=_drop_fingerprints,
    store_results=False,
)
def cluster_molecules_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    parsed = SelectionClusterJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("cluster_molecules_job requires project_db in config.")
    db_path = getattr(project_db, "db_path", None)
    project_root = ""
    if db_path:
        root = Path(db_path).expanduser().resolve().parent
        set_default_project_root(root)
        project_root = str(root)

    run_id = parsed.cluster_run_id or uuid.uuid4().hex
    run_dir = Path(project_root or ".") / "clustering_runs" / run_id
    if parsed.shards:
        fp_path, ids_path, count = _concat_shard_fingerprints(run_dir / "shards", out_dir=run_dir, nbits=parsed.fp_nbits)
    else:
        fp_path, ids_path, count = _write_packed_fingerprints(
            project_db, parsed, out_dir=run_dir, store=store_from_config(config_map),
        )
    if not count:
        return
    # A single chunk (BitBIRCH is single-pass and clustering is global) but the payload is a
    # ~300 B reference, not the library: what scales lives on disk, not in the message or RAM.
    yield {
        "fp_path": worker_file(fp_path),
        "ids_path": worker_file(ids_path),
        "count": count,
        "nbits": int(parsed.fp_nbits),
        "method": parsed.method,
        "threshold": parsed.threshold,
        "per_cluster": parsed.per_cluster,
        "num_processes": parsed.num_processes,
        "cluster_run_id": run_id,
        "output_dir": worker_output_dir(Path(project_root or ".") / "clustering_runs"),
        "fp_type": _fp_type_for_radius(parsed.fp_radius),
        "by_reference": parsed.shards,
    }


__all__ = [
    "SelectionClusterJobParams",
    "activity_values",
    "cluster_molecules_job",
    "cluster_molecules_task",
    "shard_fingerprints_job",
]
