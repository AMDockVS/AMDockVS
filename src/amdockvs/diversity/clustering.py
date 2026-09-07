"""Diversity selection over binary fingerprints (pure core).

Reduce a library's chemical redundancy: cluster molecules by fingerprint similarity, then
keep a few representatives per cluster. Pure — no DB/runtime here; it consumes 0/1 fingerprint
arrays and returns labels + representative indices + stats. The API/job layer wires files or
the project DB around it, and HTP calls it inline.

Modular by design: methods live in ``CLUSTERING_METHODS`` (name -> callable(X, **params) ->
labels). BitBIRCH-Lean is the first; Taylor-Butina / others slot in the same dict later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np

# public registry — add methods here; the API/UI enumerate this to offer choices.
CLUSTERING_METHODS: dict[str, Callable[..., np.ndarray]] = {}
_CLUSTERING_ALIASES: dict[str, Callable[..., np.ndarray]] = {}


def register_method(name: str) -> Callable[[Callable[..., np.ndarray]], Callable[..., np.ndarray]]:
    def _decorator(fn: Callable[..., np.ndarray]) -> Callable[..., np.ndarray]:
        CLUSTERING_METHODS[str(name)] = fn
        return fn

    return _decorator


# --- fingerprint helpers ------------------------------------------------------
def fp_matrix_from_bitstrings(bitstrings: Iterable[bytes | str]) -> np.ndarray:
    """Stack stored fingerprint bitstrings (ascii '0'/'1', as bytes or str) into an (n, nbits)
    uint8 0/1 matrix — the same encoding the fingerprint job writes to FingerprintRecord.

    Fills a preallocated buffer instead of ``vstack``-ing a list of rows: at 2048 bits the list +
    vstack + astype route peaked at ~3x the final matrix (6.3 kB/mol vs 2 kB/mol), which is what
    made the full-library job the memory hog rather than the clustering itself.
    """
    items = bitstrings if isinstance(bitstrings, (list, tuple)) else list(bitstrings)
    if not items:
        return np.zeros((0, 0), dtype=np.uint8)
    first = items[0]
    nbits = len(first.encode("ascii") if isinstance(first, str) else first)
    out = np.empty((len(items), nbits), dtype=np.uint8)
    for i, item in enumerate(items):
        raw = item.encode("ascii") if isinstance(item, str) else item
        out[i] = np.frombuffer(raw, dtype=np.uint8)
    out -= ord("0")  # in place: '0'/'1' ascii -> 0/1
    return out


class PackedFingerprints:
    """0/1 (n, nbits) view over a bit-packed matrix, usually a memory-mapped ``.npy``.

    Clustering only indexes rows (``x[a:b]``, ``x[idx]``), so unpacking just the requested block is
    enough: RAM is set by the block, not by library size. On disk it is 256 B/mol at 2048 bits
    against 2 kB/mol unpacked — at 33 M molecules, 8 GB mapped versus 67 GB resident. It goes
    wherever an ``np.ndarray`` used to; nothing downstream can tell the difference.
    """

    ndim = 2
    dtype = np.uint8

    def __init__(self, packed: np.ndarray, nbits: int):
        self._packed = packed
        self.shape = (int(packed.shape[0]), int(nbits))

    def __len__(self) -> int:
        return self.shape[0]

    def __getitem__(self, key: Any) -> np.ndarray:
        rows = np.unpackbits(np.asarray(self._packed[key]), axis=-1)
        return rows[..., : self.shape[1]]

    def packed_rows(self, start: int, stop: int) -> np.ndarray:
        """The rows already packed — bb wants them that way, without going through 0/1."""
        return np.asarray(self._packed[int(start):int(stop)])


def fp_matrix_from_mols(mols: Iterable[Any], *, radius: int = 2, n_bits: int = 2048) -> np.ndarray:
    """Compute a Morgan-fingerprint 0/1 matrix from RDKit mols (None rows dropped)."""
    from amdockvs.chemistry.fingerprints import morgan_fingerprint

    rows: list[str] = []
    for mol in mols:
        if mol is None:
            continue
        bits = morgan_fingerprint(mol, radius=radius, n_bits=n_bits)
        rows.append(bits.ToBitString())
    return fp_matrix_from_bitstrings(rows)


def _tanimoto_to_matrix(fp: np.ndarray, matrix: np.ndarray, matrix_sums: np.ndarray) -> np.ndarray:
    """Tanimoto of one 0/1 vector against every row of ``matrix`` (whose row sums are precomputed)."""
    inter = matrix @ fp
    denom = matrix_sums + int(fp.sum()) - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom > 0, inter / denom, 0.0)


# --- BitBIRCH-Lean ------------------------------------------------------------
@dataclass
class _ClusterFeature:
    """A BitBIRCH cluster summary: count N and the linear sum LS (per-bit counts). The centroid
    is the majority-vote bit vector (bit on where >=50% of members have it). Only N+LS are kept,
    so memory is O(nbits) per cluster regardless of size — the 'lean' part."""

    n: int
    ls: np.ndarray  # int64, length nbits

    @classmethod
    def from_fp(cls, fp: np.ndarray) -> "_ClusterFeature":
        return cls(n=1, ls=fp.astype(np.int64).copy())

    def add(self, fp: np.ndarray) -> None:
        self.n += 1
        self.ls += fp

    def centroid(self) -> np.ndarray:
        # majority rule: bit on where 2*count >= N (ties count as on -> keeps shared features).
        return (2 * self.ls >= self.n).astype(np.uint8)


# NOT registered: this is the hand-rolled stopgap kept only as the fallback used by ``bitbirch``
# when bblean is missing (and by the self-check). The user-facing method is real bblean.
def bitbirch_lean(x_matrix: np.ndarray, *, threshold: float = 0.65, **_: Any) -> np.ndarray:
    """Single-pass online clustering with BitBIRCH cluster features (N + linear sum).

    Each molecule joins the existing cluster whose majority-vote centroid is most Tanimoto-similar
    to it, provided that similarity >= ``threshold``; otherwise it seeds a new cluster. Returns a
    length-n int label array (0-based, in cluster-creation order).

    # ponytail: leaf-level greedy (no CF-tree) → O(n·k·nbits); fine to tens of thousands of mols.
    # Upgrade path if k explodes: add the BitBIRCH branching tree so lookup is ~O(log k).
    """
    n = int(x_matrix.shape[0])
    labels = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return labels
    thr = float(threshold)
    cfs: list[_ClusterFeature] = []
    cent_matrix: list[np.ndarray] = []   # cached centroids, one per cluster
    cent_sums: list[int] = []            # their bit sums
    for i in range(n):
        fp = x_matrix[i].astype(np.uint8)
        if cfs:
            mat = np.asarray(cent_matrix)
            sims = _tanimoto_to_matrix(fp, mat, np.asarray(cent_sums))
            best = int(np.argmax(sims))
            best_sim = float(sims[best])
        else:
            best, best_sim = -1, -1.0
        if best >= 0 and best_sim >= thr:
            cfs[best].add(fp)
            labels[i] = best
            new_centroid = cfs[best].centroid()
            cent_matrix[best] = new_centroid
            cent_sums[best] = int(new_centroid.sum())
        else:
            labels[i] = len(cfs)
            cfs.append(_ClusterFeature.from_fp(fp))
            cent_matrix.append(fp.copy())
            cent_sums.append(int(fp.sum()))
    return labels


# --- bblean via its CLI -------------------------------------------------------
# bblean is GPL-3.0 and AMDock is MIT, so we never ``import bblean``: we run its ``bb`` console
# script as a separate program (fork/exec, no linking) and read the pickle it leaves behind. That
# also sidesteps the deadlock multiround's multiprocessing Pool hits inside mf's loky workers.
def _bb_executable() -> str | None:
    """Path to bblean's ``bb`` console script, or None if bblean isn't installed."""
    import shutil
    import sys
    from pathlib import Path

    local = Path(sys.executable).parent / "bb"  # our own env, even when PATH is stripped
    return str(local) if local.exists() else shutil.which("bb")


def _bb_cluster_labels(
    x_matrix: np.ndarray, subcommand: str, options: list[str], *, num_files: int = 1
) -> np.ndarray:
    """Run ``bb <subcommand>`` over the fingerprint matrix and return a length-n label array.

    Bit-packs the (n, nbits) 0/1 matrix (8x less memory — matters for the full-library job) and
    writes it as ``num_files`` ``.npy`` shards; bb reads them in sorted name order, so row order
    survives into its global indices. bb leaves ``clusters.pkl`` — a list of clusters of input-row
    indices — which we invert into labels.
    """
    import pickle
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    bb = _bb_executable()
    if bb is None:
        raise RuntimeError("bblean is not installed: `pip install amdockvs-vs[bblean]` provides `bb`")
    n, nbits = int(x_matrix.shape[0]), int(x_matrix.shape[1])
    work = Path(tempfile.mkdtemp(prefix="amdock_bb_"))
    try:
        in_dir, out_dir = work / "in", work / "out"  # bb creates out_dir itself
        in_dir.mkdir()
        # Pack and write one shard at a time: packing the whole matrix first would hold a second
        # full copy alongside x_matrix, and the packed form is all bb ever reads (256 B/mol at
        # 2048 bits vs 2 kB/mol unpacked).
        # If the input is already packed (PackedFingerprints over a .npy) it is copied as-is:
        # unpacking only to repack would be the one full in-RAM copy on this path.
        packed_rows = getattr(x_matrix, "packed_rows", None)
        for k, rows in enumerate(np.array_split(np.arange(n), max(1, min(int(num_files), n)))):
            start, stop = int(rows[0]), int(rows[-1]) + 1
            block = packed_rows(start, stop) if packed_rows else np.packbits(x_matrix[start:stop], axis=-1)
            np.save(in_dir / f"shard-{k:05d}.npy", block)
        proc = subprocess.run(
            [bb, subcommand, str(in_dir), "-o", str(out_dir), "--n-features", str(nbits),
             "--packed-input", "--no-monitor-mem", "-V", *options],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"`bb {subcommand}` failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}")
        with open(out_dir / "clusters.pkl", "rb") as fh:
            clusters = pickle.load(fh)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    labels = np.full(n, -1, dtype=np.int64)
    for cluster_id, members in enumerate(clusters):
        labels[np.asarray(list(members), dtype=np.int64)] = cluster_id
    return labels


@register_method("bitbirch")
def bitbirch(
    x_matrix: np.ndarray,
    *,
    threshold: float = 0.35,
    branching_factor: int = 50,
    merge_criterion: str = "diameter",
    tolerance: float | None = None,
    **_: Any,
) -> np.ndarray:
    """Real BitBIRCH via bblean's ``bb run`` (CF-tree + iSIM, the reference implementation).

    Follows bblean's best-practice defaults: ``merge_criterion="diameter"`` and a threshold tuned
    for ECFP4 (0.3-0.4; bblean recommends 0.5-0.65 only for 'rdkit' fingerprints). Falls back to the
    lean reimplementation if bblean isn't installed, so the method is always available.
    """
    n = int(x_matrix.shape[0])
    if n == 0:
        return np.full(0, -1, dtype=np.int64)
    if _bb_executable() is None:
        return bitbirch_lean(x_matrix, threshold=threshold)
    options = ["-t", str(float(threshold)), "-b", str(int(branching_factor)), "-m", str(merge_criterion)]
    if tolerance is not None:
        options += ["--tolerance", str(float(tolerance))]
    return _bb_cluster_labels(x_matrix, "run", options)


def _cluster_features(x_matrix: np.ndarray, labels: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per cluster: (member row indices, majority-vote centroid bit vector)."""
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for label in np.unique(labels):
        idx = np.where(labels == label)[0]
        centroid = (2 * x_matrix[idx].sum(axis=0) >= len(idx)).astype(np.uint8)
        out.append((idx, centroid))
    return out


# NOT registered: superseded by real bblean (O(N), packed, has its own multiround parallelism).
# Kept as the reference two-level decomposition in case the durable job wants batch fan-out later.
def bitbirch_lean_parallel(
    x_matrix: np.ndarray, *, threshold: float = 0.65, batch_size: int = 2000, **_: Any
) -> np.ndarray:
    """Two-level BitBIRCH (the paper's parallel/iterative strategy): split into batches, cluster each
    batch independently, then cluster the batches' centroids to stitch them together. The per-batch
    step is embarrassingly parallel — the mf clustering job dispatches one batch per chunk — while
    quality stays close to the single-pass version. Returns the same length-n label array.

    Stage 1 is what parallelizes; Stage 2 (clustering ~one centroid per local cluster) is cheap.
    """
    n = int(x_matrix.shape[0])
    labels = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return labels
    bs = max(1, int(batch_size))
    if n <= bs:  # single batch → identical to the sequential method
        return bitbirch_lean(x_matrix, threshold=threshold)

    centroids: list[np.ndarray] = []
    member_groups: list[np.ndarray] = []  # global row indices behind each local centroid
    for start in range(0, n, bs):
        idx = np.arange(start, min(start + bs, n))
        local = bitbirch_lean(x_matrix[idx], threshold=threshold)
        for members, centroid in _cluster_features(x_matrix[idx], local):
            centroids.append(centroid)
            member_groups.append(idx[members])
    # Stage 2: cluster the local centroids; each maps its members to the merged global cluster.
    global_of_local = bitbirch_lean(np.vstack(centroids).astype(np.uint8), threshold=threshold)
    for global_id, members in zip(global_of_local, member_groups):
        labels[members] = int(global_id)
    return labels


# --- representative (centroid) selection --------------------------------------
def select_representatives(
    x_matrix: np.ndarray,
    labels: np.ndarray,
    *,
    per_cluster: int = 1,
) -> list[int]:
    """Pick up to ``per_cluster`` representatives per cluster: the members most Tanimoto-similar to
    their cluster's majority centroid (medoid-by-centroid). Returns global row indices, sorted."""
    reps: list[int] = []
    per_cluster = max(1, int(per_cluster))
    for label in np.unique(labels):
        member_idx = np.where(labels == label)[0]
        members = x_matrix[member_idx]
        centroid = (2 * members.sum(axis=0) >= len(member_idx)).astype(np.uint8)
        sims = _tanimoto_to_matrix(centroid, members, members.sum(axis=1))
        order = np.argsort(-sims)[:per_cluster]
        reps.extend(int(member_idx[j]) for j in order)
    return sorted(reps)


def size_histogram(sizes: Iterable[int]) -> tuple[list[str], list[int]]:
    """Bin cluster sizes into a (labels, counts) histogram for the size-distribution bar chart.
    Exact buckets for the common small sizes, coarser ranges for the heavy tail; trailing empty
    buckets are dropped so the chart ends at the largest present bucket."""
    edges = [(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5"), (10, "6-10"),
             (50, "11-50"), (100, "51-100"), (500, "101-500"), (10 ** 9, "500+")]
    counts = [0] * len(edges)
    for size in sizes:
        for i, (hi, _label) in enumerate(edges):
            if int(size) <= hi:
                counts[i] += 1
                break
    last = max((i for i, c in enumerate(counts) if c), default=-1)
    return [label for _hi, label in edges[: last + 1]], counts[: last + 1]


# --- statistics & projection --------------------------------------------------
def cluster_stats(x_matrix: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    """Compactness + separation summary. Per cluster: size and mean Tanimoto of members to their
    centroid (tightness → 1.0 = identical). Global: cluster count, singleton count, mean tightness,
    and the min inter-centroid Tanimoto distance (small = two clusters nearly redundant)."""
    unique = np.unique(labels)
    per_cluster: list[dict[str, Any]] = []
    centroids: list[np.ndarray] = []
    for label in unique:
        member_idx = np.where(labels == label)[0]
        members = x_matrix[member_idx]
        centroid = (2 * members.sum(axis=0) >= len(member_idx)).astype(np.uint8)
        centroids.append(centroid)
        tightness = float(
            np.mean(_tanimoto_to_matrix(centroid, members, members.sum(axis=1)))
        ) if len(member_idx) else 0.0
        per_cluster.append(
            {"cluster_id": int(label), "size": int(len(member_idx)), "tightness": round(tightness, 4)}
        )
    sizes = np.array([c["size"] for c in per_cluster]) if per_cluster else np.array([0])
    min_intercluster_distance = None
    if len(centroids) >= 2:
        cmat = np.asarray(centroids)
        csums = cmat.sum(axis=1)
        best = 0.0
        for i in range(len(centroids)):
            sims = _tanimoto_to_matrix(cmat[i], cmat, csums)
            sims[i] = -1.0
            best = max(best, float(sims.max()))
        min_intercluster_distance = round(1.0 - best, 4)  # closest pair distance
    return {
        "n_clusters": int(len(unique)),
        "n_molecules": int(x_matrix.shape[0]),
        "n_singletons": int(np.sum(sizes == 1)),
        "largest_cluster": int(sizes.max()),
        "mean_tightness": round(float(np.mean([c["tightness"] for c in per_cluster])), 4) if per_cluster else 0.0,
        "min_intercluster_distance": min_intercluster_distance,
        "clusters": per_cluster,
    }


def fit_project_2d(x_matrix: np.ndarray) -> tuple[np.ndarray, list[float], dict[str, Any]]:
    """Fit a 2-D PCA layout of the fingerprints and return ``(coords, evr, basis)`` — plain numpy
    SVD, no sklearn. ``basis`` (mean + top-2 components + evr) lets later samples be projected onto
    the SAME axes via ``project_2d_onto``, so successive previews overlay in one comparable space."""
    n = int(x_matrix.shape[0])
    m = int(x_matrix.shape[1]) if x_matrix.ndim == 2 else 0
    empty_basis = {"mean": np.zeros(m), "components": np.zeros((m, 2)), "evr": [0.0, 0.0]}
    if n == 0 or m == 0 or n == 1:
        return np.zeros((n, 2), dtype=float), [0.0, 0.0], empty_basis
    # `[:]` so a PackedFingerprints is accepted too; over an ndarray it is a view, not a copy.
    x = np.asarray(x_matrix[:], dtype=float)
    mean = x.mean(axis=0)
    xc = x - mean
    # economy SVD; the first two right-singular vectors are the top-2 principal axes.
    _u, s, vt = np.linalg.svd(xc, full_matrices=False)
    components = vt[:2].T if vt.shape[0] >= 2 else np.pad(vt.T, ((0, 0), (0, 2 - vt.shape[0])))
    total = float(np.sum(s ** 2)) or 1.0
    evr = [round(float(s[i] ** 2 / total), 4) if i < len(s) else 0.0 for i in range(2)]
    return xc @ components, evr, {"mean": mean, "components": components, "evr": evr}


def project_2d_onto(x_matrix: np.ndarray, basis: dict[str, Any]) -> np.ndarray:
    """Project fingerprints onto an already-fitted ``basis`` (from ``fit_project_2d``).

    Row-blocked: this is the one place that sees the *whole* library, and a single
    ``x_matrix.astype(float)`` would be a float64 copy of it — 16 kB/mol at 2048 bits, 8x the uint8
    matrix itself and the largest allocation in the clustering job. Blocking caps that at ~134 MB
    no matter how many molecules there are.
    """
    n = int(x_matrix.shape[0])
    if n == 0:
        return np.zeros((0, 2), dtype=float)
    mean = np.asarray(basis["mean"], dtype=np.float64)
    components = np.asarray(basis["components"], dtype=np.float64)
    out = np.empty((n, int(components.shape[1])), dtype=np.float64)
    block = min(4096, n)
    # One reusable float64 buffer: re-binding `x[a:b].astype(float)` each pass allocated the next
    # block before releasing the previous one, so the peak was two blocks instead of one.
    buf = np.empty((block, int(x_matrix.shape[1])), dtype=np.float64)
    for start in range(0, n, block):
        stop = min(start + block, n)
        rows = buf[: stop - start]
        np.copyto(rows, x_matrix[start:stop])
        rows -= mean
        out[start:stop] = rows @ components
    return out


def project_2d(x_matrix: np.ndarray) -> tuple[np.ndarray, list[float]]:
    """2-D PCA layout + explained-variance ratio (the fit-and-forget form used where no shared
    basis is needed). The scatter view uses ``fit_project_2d``/``project_2d_onto`` to overlay runs."""
    coords, evr, _basis = fit_project_2d(x_matrix)
    return coords, evr


# --- top-level orchestration --------------------------------------------------
@dataclass
class SelectionResult:
    labels: np.ndarray
    representative_indices: list[int]
    stats: dict[str, Any]
    projection: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    projection_variance: list[float] = field(default_factory=lambda: [0.0, 0.0])
    method: str = "bitbirch"


def cluster_multiround_labels(
    x_matrix: np.ndarray,
    *,
    threshold: float = 0.35,
    num_processes: int = 4,
    branching_factor: int = 254,
    bin_size: int = 10,
    tolerance: float = 0.05,
    merge_criterion: str = "diameter",
) -> np.ndarray:
    """Per-molecule cluster labels via bblean's **multiround** — the parallel BitBIRCH engine.

    Two facts shape the call:
    * multiround parallelises the initial round **per input file** (``min(--ps, num_files)``
      processes), so the fingerprints are sharded across ``num_processes`` files or it runs serially
      no matter how many processes you ask for.
    * multiround spawns a ``multiprocessing`` Pool internally. Doing that *inside* mf's executor
      worker (which runs under joblib/**loky**) deadlocks — forkserver children inherit loky's start
      method ('cannot find context for loky') and fork children deadlock on loky's locks. ``bb`` is a
      clean interpreter with no loky in it, so it parallelises normally.
    """
    n = int(x_matrix.shape[0])
    if n == 0:
        return np.full(0, -1, dtype=np.int64)
    nproc = max(1, int(num_processes))
    return _bb_cluster_labels(
        x_matrix,
        "multiround",
        ["-t", str(float(threshold)), "-b", str(int(branching_factor)), "-m", str(merge_criterion),
         "--tolerance", str(float(tolerance)), "--bin-size", str(int(bin_size)), "--ps", str(nproc),
         # bblean's own guidance: the midsection rounds are the memory-intensive ones, run them at
         # 50% of the initial fan-out so peak RSS stays roughly flat instead of spiking mid-run.
         "--mid-ps", str(max(1, nproc // 2))],
        num_files=nproc,  # one shard per process -> real fan-out
    )


def cluster_and_select(
    x_matrix: np.ndarray,
    *,
    method: str = "bitbirch",
    per_cluster: int = 1,
    with_projection: bool = True,
    **method_params: Any,
) -> SelectionResult:
    """Cluster ``x_matrix`` (0/1 fingerprints) with ``method`` then pick representatives.

    ``method_params`` (e.g. threshold) are passed straight to the registered method, so callers
    and the UI stay decoupled from any single method's knobs.
    """
    method_fn = CLUSTERING_METHODS.get(method) or _CLUSTERING_ALIASES.get(method)
    if method_fn is None:
        raise ValueError(f"Unknown clustering method '{method}'. Available: {sorted(CLUSTERING_METHODS)}")
    labels = method_fn(x_matrix, **method_params)
    reps = select_representatives(x_matrix, labels, per_cluster=per_cluster)
    projection, projection_variance = project_2d(x_matrix) if with_projection else (np.zeros((0, 2)), [0.0, 0.0])
    return SelectionResult(
        labels=labels,
        representative_indices=reps,
        stats=cluster_stats(x_matrix, labels),
        projection=projection,
        projection_variance=projection_variance,
        method=method,
    )


_CLUSTERING_ALIASES["bitbirch_lean"] = bitbirch_lean


__all__ = [
    "CLUSTERING_METHODS",
    "PackedFingerprints",
    "SelectionResult",
    "bitbirch",
    "bitbirch_lean",
    "bitbirch_lean_parallel",
    "cluster_and_select",
    "cluster_multiround_labels",
    "cluster_stats",
    "fit_project_2d",
    "fp_matrix_from_bitstrings",
    "fp_matrix_from_mols",
    "project_2d",
    "project_2d_onto",
    "register_method",
    "select_representatives",
    "size_histogram",
]


def _demo() -> None:
    # Two tight, well-separated blobs + one outlier -> expect ~3 clusters, correct reps.
    rng = np.random.default_rng(0)
    base_a = (rng.random(64) < 0.15).astype(np.uint8)
    base_b = (rng.random(64) < 0.15).astype(np.uint8)
    def jitter(base):
        v = base.copy()
        flip = rng.integers(0, 64, size=2)
        v[flip] ^= 1
        return v
    rows = [jitter(base_a) for _ in range(8)] + [jitter(base_b) for _ in range(8)]
    rows.append((rng.random(64) < 0.6).astype(np.uint8))  # dense outlier
    x = np.vstack(rows).astype(np.uint8)
    # Self-check the pure pieces directly (no bb needed): the lean fallback + reps + stats + PCA.
    labels = bitbirch_lean(x, threshold=0.6)
    reps = select_representatives(x, labels, per_cluster=1)
    stats = cluster_stats(x, labels)
    projection, _evr = project_2d(x)
    assert stats["n_clusters"] >= 3, stats
    assert len(reps) == stats["n_clusters"]
    assert projection.shape == (len(rows), 2)
    assert (labels >= 0).all()
    assert all(0 <= i < len(rows) for i in reps)
    # identical inputs collapse to one cluster
    same = np.vstack([base_a] * 5)
    assert cluster_stats(same, bitbirch_lean(same, threshold=0.6))["n_clusters"] == 1

    # PackedFingerprints has to be indistinguishable from the dense matrix for everything
    # downstream: same labels, same representatives, same stats, same projection.
    packed = PackedFingerprints(np.packbits(x, axis=-1), x.shape[1])
    assert packed.shape == x.shape and len(packed) == len(x)
    assert np.array_equal(packed[3], x[3])                    # single row
    assert np.array_equal(packed[2:7], x[2:7])                # slice
    assert np.array_equal(packed[[1, 9, 4]], x[[1, 9, 4]])    # fancy indexing
    assert np.array_equal(packed.packed_rows(0, 4), np.packbits(x[:4], axis=-1))
    assert np.array_equal(bitbirch_lean(packed, threshold=0.6), labels)
    assert select_representatives(packed, labels, per_cluster=1) == reps
    assert cluster_stats(packed, labels) == stats
    assert np.allclose(project_2d(packed)[0], projection)
    # nbits not a multiple of 8: packbits pads the last word and it has to be trimmed
    odd = x[:, :60]
    assert np.array_equal(PackedFingerprints(np.packbits(odd, axis=-1), 60)[:], odd)

    # size histogram: exact small buckets, tail bucketed, trailing empties dropped
    labels_h, counts_h = size_histogram([1, 1, 3, 7, 5000])
    assert labels_h == ["1", "2", "3", "4", "5", "6-10", "11-50", "51-100", "101-500", "500+"]
    assert counts_h == [2, 0, 1, 0, 0, 1, 0, 0, 0, 1]
    assert size_histogram([1, 1, 1]) == (["1"], [3])  # single bucket, no empty tail
    assert size_histogram([]) == ([], [])

    # parallel core: batch_size >= n is identical to sequential; batched keeps every mol labeled and
    # recovers the two well-separated blobs (small batches, so the split is actually exercised).
    seq = bitbirch_lean(x, threshold=0.6)
    par_full = bitbirch_lean_parallel(x, threshold=0.6, batch_size=len(rows))
    assert np.array_equal(seq, par_full), "batch_size>=n must match sequential"
    par = bitbirch_lean_parallel(x, threshold=0.6, batch_size=4)
    assert (par >= 0).all(), "every molecule labeled"
    # the two tight blobs (rows 0-7, 8-15) should each stay internally consistent
    assert len(set(par[:8].tolist())) <= 2 and len(set(par[8:16].tolist())) <= 2, par
    print("ok", stats["n_clusters"], "clusters,", len(reps), "reps",
          "| parallel:", len(np.unique(par)), "clusters")

    # multiround (real bblean via its `bb` CLI) — only if installed; the two blobs must come back
    # as separate clusters with every molecule labeled.
    if _bb_executable() is None:
        print("multiround: skipped (bblean not installed)")
    else:
        mr = cluster_multiround_labels(x, threshold=0.6, num_processes=2, bin_size=4)
        assert (mr >= 0).all(), "multiround: every molecule labeled"
        assert len(np.unique(mr)) >= 2, ("multiround should find >=2 clusters", np.unique(mr))
        print("multiround: ok", len(np.unique(mr)), "clusters")


if __name__ == "__main__":
    _demo()
