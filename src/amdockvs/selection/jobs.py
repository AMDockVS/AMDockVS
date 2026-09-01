"""Diversity-selection job — cluster the library's fingerprints and persist assignments.

Async over the project DB via MolSuite: the job function reads fingerprints through the
MolSuite query API (``db_rows``) on the orchestrator side (where project_db lives), hands
one payload to a pure-compute task, and the task's rows land in the ``clustering_results`` table
through a MolSuite ``table_sink``. No worker-side DB connections — input and output both go
through MolSuite, so there is no direct project.db access from inside a task.
"""
from __future__ import annotations

import uuid
from itertools import batched
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec, db_count, db_pages, db_rows
from ms_flow.sinks import table_sink
from ms_flow.tasking import job, task

from amdockvs.configuration import batch_size_for
from amdockvs.api_common import worker_file, worker_output_dir
from amdockvs.constants import (
    AMDOCKVS_LOCAL_EXECUTORS,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
from amdockvs.models import ClusteringResult
from amdockvs.models.descriptors import FingerprintType
from amdockvs.docking.repository import molecule_set_spec
from amdockvs.molecule_paths import preferred_molecule_path, set_default_project_root
from amdockvs.selection.clustering import (
    PackedFingerprints,
    cluster_and_select,
    cluster_multiround_labels,
    fp_matrix_from_bitstrings,
    select_representatives,
)


def _fp_type_for_radius(radius: int) -> str:
    return FingerprintType.ECFP6 if int(radius) >= 3 else FingerprintType.ECFP4


class SelectionClusterJobParams(BaseModel):
    method: str = Field(default="bitbirch")
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    per_cluster: int = Field(default=1, ge=1)
    molecule_set_id: int | None = Field(default=None, ge=1)
    molecule_filters: dict[str, Any] = Field(default_factory=dict)
    fp_radius: int = Field(default=2, ge=1)
    fp_nbits: int = Field(default=2048, ge=64)
    cluster_run_id: str = Field(default="")
    num_processes: int = Field(default=1, ge=1)  # >1 → bblean multiround (parallel); matches cpu_required


def _scope_spec(params: SelectionClusterJobParams) -> QuerySpec:
    """The scope to cluster, resolved entirely in SQL. Active ligands by default.

    Set membership is a subquery, not a Python `set` holding every id in the set: that set
    grew with the library and also forced row-by-row filtering outside the db.
    """
    filters = {"is_ligand": True, "excluded": False, "stored_path__ne": "", **dict(params.molecule_filters or {})}
    filters.pop("_limit", None)
    if params.molecule_set_id is not None:
        filters["id__in_subquery"] = molecule_set_spec(int(params.molecule_set_id))
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "stored_path", "current_path", "current_model_index", "input_format"),
        filters=filters,
        order=("id",),
    )


def scope_molecule_rows(project_db, params: SelectionClusterJobParams) -> Iterator[dict[str, Any]]:
    """The molecules in the scope, in keyset pages. Flat rows: whoever wants to wrap them
    does so on the consumer side."""
    return db_pages(project_db, _scope_spec(params), page_size=batch_size_for("ligand"))


def scope_molecule_count(project_db, params: SelectionClusterJobParams) -> int:
    """How many molecules the scope holds — a COUNT, not a walk over the whole library."""
    return db_count(project_db, _scope_spec(params))


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


def _fingerprint_from_file(row: dict[str, Any], *, radius: int, nbits: int) -> bytes | None:
    """Compute a Morgan fingerprint bitstring for a molecule whose FingerprintRecord is missing.
    Fallback path so selection works without a prior fingerprint job (slower — serial, in-process)."""
    from rdkit import Chem

    from amdockvs.chemistry.fingerprints import morgan_fingerprint

    path = preferred_molecule_path(row)
    if path is None or not Path(path).exists():
        return None
    mol = Chem.MolFromMolFile(str(path), sanitize=True, removeHs=False)
    if mol is None:
        return None
    return morgan_fingerprint(mol, radius=int(radius), n_bits=int(nbits)).ToBitString().encode("ascii")


def _write_packed_fingerprints(
    project_db, params: SelectionClusterJobParams, *, out_dir: Path
) -> tuple[Path, Path, int]:
    """Dumps the scope to ``fingerprints.npy`` (bit-packed) + ``ids.npy``, in batches.

    The files are preallocated from the scope COUNT and filled through a memmap, so only one
    batch lives in RAM at a time: usage does not depend on library size. Returns
    ``(fp_path, ids_path, written)``; rows without a usable fingerprint are skipped, which is
    why the counter can end below the COUNT and the leftover tails are never read.
    """
    import numpy as np

    total = scope_molecule_count(project_db, params)
    out_dir.mkdir(parents=True, exist_ok=True)
    fp_path, ids_path = out_dir / "fingerprints.npy", out_dir / "ids.npy"
    if not total:
        return fp_path, ids_path, 0

    nbits = int(params.fp_nbits)
    packed = np.lib.format.open_memmap(fp_path, mode="w+", dtype=np.uint8, shape=(total, (nbits + 7) // 8))
    ids_out = np.lib.format.open_memmap(ids_path, mode="w+", dtype=np.int64, shape=(total,))
    written = 0
    try:
        for batch in batched(scope_molecule_rows(project_db, params), batch_size_for("ligand")):
            ids = [int(row["id"]) for row in batch]
            # bounded `molecule_id__in` per batch instead of a scan of the whole fingerprint table
            stored = stored_fingerprints_for_ids(project_db, ids, radius=params.fp_radius, nbits=nbits)
            keep_ids: list[int] = []
            bitstrings: list[bytes] = []
            for molecule_id, row in zip(ids, batch):
                fp = stored.get(molecule_id) or _fingerprint_from_file(row, radius=params.fp_radius, nbits=nbits)
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


@task(
    name="amdock_cluster_molecules_batch",
    description="Cluster molecule fingerprints and emit clustering_results rows.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def cluster_molecules_task(payload: dict) -> list[dict]:
    import numpy as np

    # The payload carries paths, not fingerprints: the matrix lives bit-packed in a memory-mapped
    # .npy (256 B/mol) and is unpacked in blocks. Sending the '0'/'1' strings inside the payload was
    # 2 kB/mol serialised — 69 GB at 33 M molecules, before any clustering even started.
    count = int(payload.get("count") or 0)
    if not count:
        return []
    molecule_ids = np.load(payload["ids_path"], mmap_mode="r")[:count]
    packed = np.load(payload["fp_path"], mmap_mode="r")[:count]
    x_matrix = PackedFingerprints(packed, int(payload.get("nbits") or 0))
    per_cluster = int(payload.get("per_cluster") or 1)
    threshold = float(payload.get("threshold") or 0.65)
    num_processes = int(payload.get("num_processes") or 1)
    if num_processes > 1:
        # Large scope: parallel BitBIRCH (bblean multiround) across the CPUs mf reserved for us.
        labels = cluster_multiround_labels(x_matrix, threshold=threshold, num_processes=num_processes)
        rep_indices = select_representatives(x_matrix, labels, per_cluster=per_cluster)
    else:
        result = cluster_and_select(
            x_matrix,
            method=str(payload.get("method") or "bitbirch"),
            per_cluster=per_cluster,
            with_projection=False,  # projection is a UI concern; keep the job payload lean
            threshold=threshold,
        )
        labels, rep_indices = result.labels, result.representative_indices
    centroids = set(rep_indices)
    # Compute the 2-D PCA graph NOW (we already hold the fingerprints + labels) and persist it to the
    # run's parquet sidecar, so viewing the saved result never recomputes anything — the plot + cluster
    # sizes are read straight back. Best-effort: a sidecar failure must not lose the clustering rows.
    _write_graph_sidecar(
        output_dir=str(payload.get("output_dir") or ""),
        run_id=str(payload.get("cluster_run_id") or ""),
        x_matrix=x_matrix, molecule_ids=molecule_ids, labels=labels, centroids=centroids,
    )
    # ponytail: assignments are returned as a list — that is the task contract, and the sink
    # writes them in bulk. It is the last point proportional to n on this path (~300 B/mol);
    # if it gets in the way, the next step is a batched sink, not another structure here.
    assignments = [
        {"molecule_id": int(molecule_ids[i]), "cluster_id": int(labels[i]), "is_centroid": i in centroids}
        for i in range(count)
    ]
    rows = ClusteringResult.build_rows(
        cluster_run_id=str(payload.get("cluster_run_id") or ""),
        assignments=assignments,
        method=str(payload.get("method") or "bitbirch"),
        fp_type=str(payload.get("fp_type") or ""),
    )
    # Fingerprints are derivable: they are deleted at the end so no run leaves 256 B/mol behind.
    for key in ("fp_path", "ids_path"):
        Path(str(payload.get(key) or "")).unlink(missing_ok=True)
    return rows


def _write_graph_sidecar(
    *, output_dir: str, run_id: str, x_matrix, molecule_ids: list[int], labels, centroids: set,
) -> None:
    """Write the run's chemical-universe graph (x, y, molecule_id, cluster_id, is_centroid) to
    ``<project_root>/clustering_runs/<run_id>.parquet``. The PCA basis is fitted on a sub-sample and
    every point projected onto it (cheap). Computed here, once, so the GUI never recomputes it."""
    if not output_dir or not run_id:
        return
    try:
        import random

        import numpy as np
        import pandas as pd

        from amdockvs.selection.clustering import fit_project_2d, project_2d_onto

        n = len(molecule_ids)
        # sample straight off range(): materialising n boxed ints just to draw 2000 of them was
        # pure waste on a million-molecule library.
        fit_idx = list(range(n)) if n <= 2000 else random.Random(0).sample(range(n), 2000)
        _coords, _evr, basis = fit_project_2d(x_matrix[fit_idx])
        proj = project_2d_onto(x_matrix, basis)
        # numpy columns, not list comprehensions: boxing 5 x n Python floats/ints/bools cost more
        # RAM than the fingerprint matrix they came from, and pandas takes the arrays as they are.
        is_centroid = np.zeros(n, dtype=bool)
        if centroids:
            is_centroid[np.fromiter(centroids, dtype=np.int64, count=len(centroids))] = True
        frame = pd.DataFrame({
            "x": np.asarray(proj[:, 0], dtype=np.float64),
            "y": np.asarray(proj[:, 1], dtype=np.float64),
            "molecule_id": np.asarray(molecule_ids, dtype=np.int64),
            "cluster_id": np.asarray(labels, dtype=np.int64),
            "is_centroid": is_centroid,
        })
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(out_dir / f"{run_id}.parquet", index=False)
    except Exception:  # noqa: BLE001 — the graph is a nicety; never fail the clustering over it
        pass


@job(
    task=cluster_molecules_task,
    name="amdock_cluster_molecules_job",
    params_model=SelectionClusterJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=table_sink(model=ClusteringResult, write_mode="bulk"),
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
    fp_path, ids_path, count = _write_packed_fingerprints(
        project_db, parsed, out_dir=Path(project_root or ".") / "clustering_runs" / run_id
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
    }


__all__ = [
    "SelectionClusterJobParams",
    "cluster_molecules_job",
    "cluster_molecules_task",
]
