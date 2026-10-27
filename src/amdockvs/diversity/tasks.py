"""What a worker runs to cluster: the packed fingerprints of a run into labels, rankings and its graph.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from pathlib import Path

from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.core.jobs import task
from amdockvs.core.rows import clustering_rows
from amdockvs.diversity.clustering import (
    PackedFingerprints,
    cluster_labels,
    cluster_multiround_labels,
    member_rankings,
    pick_representatives,
)


@task(
    name="amdock_cluster_molecules_batch",
    description="Cluster molecule fingerprints and emit clustering_results rows.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def cluster_molecules_task(payload: dict) -> dict | list:
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
    method = str(payload.get("method") or "bitbirch")
    if num_processes > 1:
        # Large scope: parallel BitBIRCH (bblean multiround) across the CPUs mf reserved for us.
        labels = cluster_multiround_labels(x_matrix, threshold=threshold, num_processes=num_processes)
    else:
        labels = cluster_labels(x_matrix, method=method, threshold=threshold)
    # Ranked once, here, while the fingerprints are at hand: picking representatives is a later,
    # separate step (DiversityAPI.pick_representatives) that only reads these two columns.
    similarity, centrality = member_rankings(x_matrix, labels)
    rep_indices = pick_representatives(labels, similarity, per_cluster=per_cluster)
    centroids = set(rep_indices)
    # Compute the 2-D PCA graph NOW (we already hold the fingerprints + labels) and persist it to the
    # run's parquet sidecar, so viewing the saved result never recomputes anything — the plot + cluster
    # sizes are read straight back. Best-effort: a sidecar failure must not lose the clustering rows.
    _write_graph_sidecar(
        output_dir=str(payload.get("output_dir") or ""),
        run_id=str(payload.get("cluster_run_id") or ""),
        x_matrix=x_matrix, molecule_ids=molecule_ids, labels=labels, centroids=centroids,
        similarity=similarity, centrality=centrality,
    )
    # ponytail: assignments are returned as a list — that is the task contract, and the sink
    # writes them in bulk. It is the last point proportional to n on this path (~300 B/mol);
    # if it gets in the way, the next step is a batched sink, not another structure here.
    # A run over shards keeps its members by reference (the sidecar): only the representatives
    # become rows, and the job's sink promotes them.
    members = sorted(centroids) if payload.get("by_reference") else range(count)
    assignments = [
        {"molecule_id": int(molecule_ids[i]), "cluster_id": int(labels[i]), "is_centroid": i in centroids}
        for i in members
    ]
    rows = clustering_rows(
        cluster_run_id=str(payload.get("cluster_run_id") or ""),
        assignments=assignments,
        method=method,
        fp_type=str(payload.get("fp_type") or ""),
    )
    # Fingerprints are derivable: they are deleted at the end so no run leaves 256 B/mol behind.
    for key in ("fp_path", "ids_path"):
        Path(str(payload.get(key) or "")).unlink(missing_ok=True)
    # The sidecar is named beside the rows: a result names a file for it to come back from a disk
    # that is not this one.
    sidecar = Path(str(payload.get("output_dir") or "")) / f"{payload.get('cluster_run_id') or ''}.parquet"
    return {"rows": rows, "sidecar": str(sidecar)}


@task(
    name="amdock_shard_fingerprints",
    description="Pack the Morgan fingerprints of one library shard.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def shard_fingerprints_task(payload: dict) -> dict:
    """One shard's records as `<shard id>.fp.npy` (bit-packed) + `<shard id>.ids.npy` (their
    `shard_key`s): the part of a run's fingerprint matrix this shard owns. A record that does not
    parse is left out, as a row without a structure is."""
    import numpy as np

    from amdockvs.chemistry.fingerprints import morgan_fingerprint
    from amdockvs.chemistry.shards import read_shard
    from amdockvs.diversity.clustering import fp_matrix_from_bitstrings
    from amdockvs.io.shards import shard_key

    ids, mols = read_shard(payload["shard_path"])
    radius, nbits = int(payload["radius"]), int(payload["nbits"])
    keys, bitstrings = [], []
    for record_id, mol in zip(ids, mols):
        if mol is not None:
            keys.append(shard_key(payload.get("import_batch_id"), record_id))
            bitstrings.append(morgan_fingerprint(mol, radius=radius, n_bits=nbits).ToBitString())
    out_dir = Path(str(payload["output_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / str(int(payload["shard_id"]))
    packed = np.packbits(fp_matrix_from_bitstrings(bitstrings), axis=-1) if keys else np.zeros((0, (nbits + 7) // 8), np.uint8)
    np.save(f"{stem}.fp.npy", packed)
    np.save(f"{stem}.ids.npy", np.asarray(keys, dtype=np.int64))
    return {"fp_path": f"{stem}.fp.npy", "ids_path": f"{stem}.ids.npy", "count": len(keys)}


def _write_graph_sidecar(
    *, output_dir: str, run_id: str, x_matrix, molecule_ids: list[int], labels, centroids: set,
    similarity=None, centrality=None,
) -> None:
    """Write the run's chemical-universe graph (x, y, molecule_id, cluster_id, is_centroid) and each
    member's rankings (similarity, centrality: what representatives are picked from later) to
    ``<project_root>/clustering_runs/<run_id>.parquet``. The PCA basis is fitted on a sub-sample and
    every point projected onto it (cheap). Computed here, once, so the GUI never recomputes it."""
    if not output_dir or not run_id:
        return
    try:
        import random

        import numpy as np
        import pandas as pd

        from amdockvs.diversity.clustering import fit_project_2d, project_2d_onto

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
            "similarity": np.zeros(n, dtype=np.float32) if similarity is None else similarity,
            "centrality": np.full(n, np.nan, dtype=np.float32) if centrality is None else centrality,
        })
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(out_dir / f"{run_id}.parquet", index=False)
    except Exception:  # noqa: BLE001 — the graph is a nicety; never fail the clustering over it
        pass
