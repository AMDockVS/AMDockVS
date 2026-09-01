"""Selection API — diversity/redundancy reduction over the project library.

Two entry points around the same pure core (``amdockvs.selection.clustering``):

* ``cluster_job`` submits the durable async job that clusters the DB library and writes
  ``clustering_results`` through MolSuite sinks. Use it in HTP / workflow pipelines.
* ``analyze`` loads fingerprints via the MolSuite query API and clusters them inline (off the
  GUI thread), returning labels + representatives + stats + a 2-D projection for the interactive
  view. Nothing here opens the DB directly — reads go through ``db_rows``, writes (the
  centroid set) go through ``amdockvs.scopes``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from sqlmodel import select

from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.models import ClusteringResult, ClusteringRun, MoleculeRecord
from amdockvs.models.sets import SetPurpose
from amdockvs.scopes import MoleculeSetRef, create_snapshot_set
from amdockvs.selection.clustering import CLUSTERING_METHODS, cluster_and_select
from amdockvs.selection.jobs import (
    SelectionClusterJobParams,
    _fingerprint_from_file,
    scope_molecule_count,
    scope_molecule_rows,
    cluster_molecules_job,
    stored_fingerprints_for_ids,
)

# Interactive preview never clusters more than this inline; larger scopes are sampled for the view
# and the full library is handed to MolSuite via ``cluster_job``. Full clustering of millions of
# molecules is minutes-to-hours (linear in N) — it does not belong on any UI-side thread.
DEFAULT_SAMPLE_LIMIT = 2000

# ``run_selection`` clusters a scope inline (off the GUI thread) — a scripting/small-scope helper.
# The interactive Run always goes through the parallel job (``cluster_job``); this is only a safety
# net so a direct inline call on a huge scope fails loudly instead of hanging.
INLINE_RUN_LIMIT = 50000
# The PCA graph basis is fitted on this many random molecules (SVD is superlinear in N), then every
# point is projected onto it — a cheap matmul — so the graph scales while clustering stays full-scope.
PROJECT_FIT_SAMPLE = 2000

# CPU/parallelism policy for the Run job: one CPU per this many molecules (hybrid — the UI suggests
# ``plan_cpus(n)`` and lets the user override), capped at the machine's cores. 1 CPU → single-tree
# BitBIRCH; >1 → bblean multiround with that many processes, and the job requests exactly that many.
MOLECULES_PER_CPU = 25000


def _reservoir_sample(
    stream: Iterable[Any],
    *,
    limit: int,
    seed: int,
    skip: set[int],
) -> tuple[list[Any], int, int]:
    """``limit`` rows drawn uniformly from a stream, plus (total seen, eligible after ``skip``).

    Reservoir sampling because the preview needs a random subset of a scope that may be millions
    of rows: `random.sample` would have to hold the whole scope first, which is exactly what the
    preview is trying to avoid. Memory is `limit` rows, whatever the scope size.
    """
    import random

    rng = random.Random(int(seed))
    reservoir: list[Any] = []
    total = 0
    eligible = 0
    for row in stream:
        total += 1
        if int(row["id"]) in skip:
            continue
        eligible += 1
        if len(reservoir) < limit:
            reservoir.append(row)
            continue
        position = rng.randrange(eligible)
        if position < limit:
            reservoir[position] = row
    return reservoir, total, eligible


def plan_cpus(n: int, *, molecules_per_cpu: int = MOLECULES_PER_CPU, cap: int | None = None) -> int:
    """Suggested CPU count (= multiround processes) for clustering ``n`` molecules: ceil(n / chunk),
    at least 1, capped at the machine's cores. Deterministic, known at submit time so the job can
    request exactly this many CPUs."""
    import math
    import os

    if cap is None:
        cap = os.cpu_count() or 1
    return max(1, min(int(cap), math.ceil(max(0, int(n)) / max(1, int(molecules_per_cpu)))))


@dataclass
class UniverseMatrix:
    """The loaded (possibly sampled) fingerprint matrix + its 2-D projection for one scope. Held by
    the view so re-clustering at a new threshold never re-reads the DB — only the cheap in-memory
    cluster step re-runs. Not JSON-able (carries the numpy matrix); stays in-process."""

    molecule_ids: list[int]
    x_matrix: Any  # np.ndarray (n, nbits)
    projection: list[list[float]]
    projection_variance: list[float]
    total_in_scope: int
    sampled: bool
    basis: Any = None  # fitted PCA basis (mean+components+evr) so later previews share the axes


@dataclass
class AnalysisResult:
    """In-memory clustering result for the interactive view (JSON-able via ``to_mapping``)."""

    molecule_ids: list[int]
    labels: list[int]
    representative_ids: list[int]
    stats: dict[str, Any]
    projection: list[list[float]]
    projection_variance: list[float]
    method: str
    threshold: float
    total_in_scope: int = 0
    sampled: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "molecule_ids": self.molecule_ids,
            "labels": self.labels,
            "representative_ids": self.representative_ids,
            "stats": self.stats,
            "projection": self.projection,
            "projection_variance": self.projection_variance,
            "method": self.method,
            "threshold": self.threshold,
            "total_in_scope": self.total_in_scope,
            "sampled": self.sampled,
        }


@dataclass
class SelectionAPI:
    runtime: Any

    @staticmethod
    def supported_methods() -> tuple[str, ...]:
        return tuple(sorted(CLUSTERING_METHODS))

    # --- durable DB-async job -------------------------------------------------
    def cluster_job(
        self,
        *,
        method: str = "bitbirch",
        threshold: float = 0.65,
        per_cluster: int = 1,
        molecule_set: MoleculeSetRef | int | None = None,
        molecule_filters: dict[str, Any] | None = None,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        cluster_run_id: str = "",
        num_cpus: int | None = None,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
    ) -> str:
        """Submit the clustering job over the DB; results land in ``clustering_results``. Returns
        the job id. The run is tagged with ``cluster_run_id`` (auto if blank) — read it back with
        ``list_runs`` / ``get_run`` or persist its centroids with ``save_centroids_as_set``.

        ``num_cpus`` sets the parallelism: >1 runs bblean multiround with that many processes and the
        job **requests exactly that many CPUs** (``cpu_required``) so mf schedules it correctly. When
        None it is derived from the scope size via ``plan_cpus``."""
        self.runtime._require_active_project()
        if num_cpus is None:
            num_cpus = plan_cpus(
                self.scope_count(
                    molecule_set=molecule_set, molecule_filters=molecule_filters,
                    fp_radius=fp_radius, fp_nbits=fp_nbits,
                )
            )
        num_cpus = max(1, int(num_cpus))
        params = SelectionClusterJobParams(
            method=method,
            threshold=threshold,
            per_cluster=per_cluster,
            molecule_set_id=None if molecule_set is None else int(getattr(molecule_set, "id", molecule_set)),
            molecule_filters=dict(molecule_filters or {}),
            fp_radius=fp_radius,
            fp_nbits=fp_nbits,
            cluster_run_id=cluster_run_id or uuid.uuid4().hex,
            num_processes=num_cpus,
        )
        return self.runtime.submit_job(
            cluster_molecules_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            cpu_required=num_cpus,
            depends_on=depends_on,
        )

    # --- inline preview: load once (sampled+projected), cluster many times ----
    def load_universe(
        self,
        *,
        molecule_set: MoleculeSetRef | int | None = None,
        molecule_filters: dict[str, Any] | None = None,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        seed: int = 0,
        basis: Any = None,
        exclude_ids: Any = None,
    ) -> UniverseMatrix:
        """Load a scope's fingerprint matrix + 2-D projection for the interactive view. Samples a
        random ``sample_limit`` subset (seeded by ``seed`` so each preview draws a *fresh* subset),
        skipping ``exclude_ids`` (molecules already previewed) so repeated previews cover more of
        the space. Projects onto ``basis`` when given (a prior preview's fitted PCA axes) so runs
        overlay in one comparable space; otherwise fits a new basis and returns it. Heavy (DB read +
        PCA); call off the GUI thread. Cheap re-clustering then uses ``cluster_loaded``."""
        import random

        from amdockvs.selection.clustering import fit_project_2d, fp_matrix_from_bitstrings, project_2d_onto

        self.runtime._require_active_project()
        params = SelectionClusterJobParams(
            molecule_set_id=None if molecule_set is None else int(getattr(molecule_set, "id", molecule_set)),
            molecule_filters=dict(molecule_filters or {}),
            fp_radius=fp_radius,
            fp_nbits=fp_nbits,
        )
        project_db = self.runtime.molsuite.project_db
        skip = {int(i) for i in (exclude_ids or ())}
        rows, total, eligible = _reservoir_sample(
            scope_molecule_rows(project_db, params),
            limit=int(sample_limit),
            seed=int(seed),
            skip=skip,
        )
        sampled = eligible > int(sample_limit)
        stored = stored_fingerprints_for_ids(
            project_db, [int(r["id"]) for r in rows], radius=fp_radius, nbits=fp_nbits
        )
        ordered_ids: list[int] = []
        bitstrings: list[bytes] = []
        for row in rows:
            mol_id = int(row["id"])
            fp = stored.get(mol_id) or _fingerprint_from_file(row, radius=fp_radius, nbits=fp_nbits)
            if fp is None:
                continue
            ordered_ids.append(mol_id)
            bitstrings.append(fp)
        x_matrix = fp_matrix_from_bitstrings(bitstrings)
        if basis is None:
            projection, evr, basis = fit_project_2d(x_matrix)
        else:
            projection, evr = project_2d_onto(x_matrix, basis), list(basis.get("evr") or [0.0, 0.0])
        return UniverseMatrix(
            molecule_ids=ordered_ids,
            x_matrix=x_matrix,
            projection=[[float(x), float(y)] for x, y in projection],
            projection_variance=list(evr),
            total_in_scope=total,
            sampled=sampled,
            basis=basis,
        )

    @staticmethod
    def cluster_loaded(
        universe: UniverseMatrix,
        *,
        method: str = "bitbirch",
        per_cluster: int = 1,
        **method_params: Any,
    ) -> AnalysisResult:
        """Cluster an already-loaded ``UniverseMatrix`` — the cheap step that re-runs on every knob
        change without touching the DB. ``method_params`` (threshold, batch_size, …) go straight to
        the registered method, so each method's own knobs pass through untouched."""
        threshold = float(method_params.get("threshold", 0.65))
        if not universe.molecule_ids:
            return AnalysisResult(
                [], [], [], {"n_clusters": 0, "n_molecules": 0, "clusters": []}, [], [0.0, 0.0],
                method, threshold, universe.total_in_scope, universe.sampled,
            )
        result = cluster_and_select(
            universe.x_matrix, method=method, per_cluster=per_cluster, with_projection=False, **method_params
        )
        rep_ids = [universe.molecule_ids[i] for i in result.representative_indices]
        return AnalysisResult(
            molecule_ids=universe.molecule_ids,
            labels=[int(v) for v in result.labels],
            representative_ids=rep_ids,
            stats=result.stats,
            projection=universe.projection,
            projection_variance=universe.projection_variance,
            method=method,
            threshold=threshold,
            total_in_scope=universe.total_in_scope,
            sampled=universe.sampled,
        )

    def analyze(
        self,
        *,
        method: str = "bitbirch",
        threshold: float = 0.65,
        per_cluster: int = 1,
        molecule_set: MoleculeSetRef | int | None = None,
        molecule_filters: dict[str, Any] | None = None,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    ) -> AnalysisResult:
        """Convenience: ``load_universe`` then ``cluster_loaded`` in one call (scripting/preview)."""
        universe = self.load_universe(
            molecule_set=molecule_set,
            molecule_filters=molecule_filters,
            fp_radius=fp_radius,
            fp_nbits=fp_nbits,
            sample_limit=sample_limit,
        )
        return self.cluster_loaded(universe, method=method, threshold=threshold, per_cluster=per_cluster)

    def scope_count(
        self,
        *,
        molecule_set: MoleculeSetRef | int | None = None,
        molecule_filters: dict[str, Any] | None = None,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
    ) -> int:
        """How many molecules a scope holds — the cheap count used to route serial vs. parallel
        (no fingerprint load, no clustering). Reads ids only via the MolSuite query API."""
        self.runtime._require_active_project()
        params = SelectionClusterJobParams(
            molecule_set_id=None if molecule_set is None else int(getattr(molecule_set, "id", molecule_set)),
            molecule_filters=dict(molecule_filters or {}),
            fp_radius=fp_radius,
            fp_nbits=fp_nbits,
        )
        return scope_molecule_count(self.runtime.molsuite.project_db, params)

    def run_selection(
        self,
        *,
        method: str = "bitbirch",
        per_cluster: int = 1,
        molecule_set: MoleculeSetRef | int | None = None,
        molecule_filters: dict[str, Any] | None = None,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        **method_params: Any,
    ) -> dict[str, Any]:
        """Cluster the ENTIRE scope (no sampling) and pick representatives — the real 'run the
        selection' behind the interactive Preview. Every molecule's fingerprint is loaded and
        clustered (bblean is fast); only the 2-D graph basis is fitted on a random sub-sample and
        every point projected onto it (cheap), so the plot scales without the full-N SVD. Returns the
        same mapping as ``cluster_loaded``, over the whole scope. Heavy — call off the GUI thread.
        Raises if the scope exceeds ``INLINE_RUN_LIMIT`` (that needs the parallel job)."""
        import random

        from amdockvs.selection.clustering import (
            fit_project_2d,
            fp_matrix_from_bitstrings,
            project_2d_onto,
        )

        self.runtime._require_active_project()
        params = SelectionClusterJobParams(
            molecule_set_id=None if molecule_set is None else int(getattr(molecule_set, "id", molecule_set)),
            molecule_filters=dict(molecule_filters or {}),
            fp_radius=fp_radius,
            fp_nbits=fp_nbits,
        )
        project_db = self.runtime.molsuite.project_db
        # Stop one row past the limit instead of loading the scope first: a scope too big for the
        # inline path must not be materialized just to be rejected.
        rows = list(islice(scope_molecule_rows(project_db, params), INLINE_RUN_LIMIT + 1))
        if len(rows) > INLINE_RUN_LIMIT:
            raise ValueError(
                f"More than {INLINE_RUN_LIMIT} molecules exceed the serial limit — this scope belongs "
                f"on the parallel clustering job (cluster_job), not the inline path."
            )
        total = len(rows)
        stored = stored_fingerprints_for_ids(
            project_db, [int(r["id"]) for r in rows], radius=fp_radius, nbits=fp_nbits
        )
        ordered_ids: list[int] = []
        bitstrings: list[bytes] = []
        for row in rows:
            mol_id = int(row["id"])
            fp = stored.get(mol_id) or _fingerprint_from_file(row, radius=fp_radius, nbits=fp_nbits)
            if fp is None:
                continue
            ordered_ids.append(mol_id)
            bitstrings.append(fp)
        x_matrix = fp_matrix_from_bitstrings(bitstrings)
        universe = UniverseMatrix(
            molecule_ids=ordered_ids, x_matrix=x_matrix, projection=[],
            projection_variance=[0.0, 0.0], total_in_scope=total, sampled=False,
        )
        n = len(ordered_ids)
        if n:
            fit_idx = list(range(n))
            if n > PROJECT_FIT_SAMPLE:
                fit_idx = random.Random(0).sample(fit_idx, PROJECT_FIT_SAMPLE)
            _coords, evr, basis = fit_project_2d(x_matrix[fit_idx])
            proj = project_2d_onto(x_matrix, basis)
            universe.projection = [[float(x), float(y)] for x, y in proj]
            universe.projection_variance = list(evr)
        return self.cluster_loaded(
            universe, method=method, per_cluster=per_cluster, **method_params
        ).to_mapping()

    def register_run_from_sidecar(
        self,
        cluster_run_id: str,
        *,
        method: str,
        threshold: float,
        scope_label: str,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
    ) -> str:
        """Register a finished parallel run as a saved result WITHOUT recomputing anything: the job's
        task already wrote the graph sidecar ``<root>/clustering_runs/<run_id>.parquet`` (PCA points +
        cluster/centroid flags). Read it for the counts + aggregated cluster sizes and insert the
        compact ``ClusteringRun`` summary pointing at it. No fingerprints, no PCA here."""
        import pandas as pd

        self.runtime._require_active_project()
        snapshot = self._project_root() / "clustering_runs" / f"{cluster_run_id}.parquet"
        if snapshot.exists():
            frame = pd.read_parquet(snapshot)
            n_molecules = int(len(frame))
            n_clusters = int(frame["cluster_id"].nunique()) if n_molecules else 0
            n_reps = int(frame["is_centroid"].sum()) if n_molecules else 0
            cluster_stats = [
                {"cluster_id": int(cid), "size": int(size), "tightness": 0.0}
                for cid, size in frame.groupby("cluster_id").size().items()
            ]
            snapshot_path = str(snapshot)
        else:  # sidecar missing (e.g. write failed) → counts-only summary, no graph
            n_molecules = n_clusters = n_reps = 0
            cluster_stats = []
            snapshot_path = ""
        row = ClusteringRun(
            run_id=cluster_run_id, method=str(method), threshold=float(threshold),
            scope_label=str(scope_label), fp_radius=int(fp_radius), fp_nbits=int(fp_nbits),
            n_molecules=n_molecules, n_clusters=n_clusters, n_reps=n_reps,
            snapshot_path=snapshot_path, evr=[0.0, 0.0], cluster_stats=cluster_stats,
        )
        with self.runtime.molsuite.project_db.get_session() as session:
            session.add(row)
            session.commit()
        return cluster_run_id

    # --- reading persisted runs ----------------------------------------------
    def list_runs(self) -> list[dict[str, Any]]:
        """Persisted clustering runs, newest first: run id, method, cluster/centroid counts."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(select(ClusteringResult)).all()
        runs: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = runs.setdefault(
                row.cluster_run_id,
                {"cluster_run_id": row.cluster_run_id, "method": row.method, "created_at": row.created_at,
                 "n_molecules": 0, "n_clusters": set(), "n_centroids": 0},
            )
            entry["n_molecules"] += 1
            entry["n_clusters"].add(int(row.cluster_id))
            if row.is_centroid:
                entry["n_centroids"] += 1
        out = []
        for entry in runs.values():
            entry["n_clusters"] = len(entry["n_clusters"])
            out.append(entry)
        return sorted(out, key=lambda e: str(e.get("created_at") or ""), reverse=True)

    def get_run(self, cluster_run_id: str) -> list[dict[str, Any]]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(
                select(ClusteringResult).where(ClusteringResult.cluster_run_id == str(cluster_run_id))
            ).all()
        return [
            {"molecule_id": int(r.molecule_id), "cluster_id": int(r.cluster_id), "is_centroid": bool(r.is_centroid)}
            for r in rows
        ]

    def centroid_ids(self, cluster_run_id: str) -> list[int]:
        return [r["molecule_id"] for r in self.get_run(cluster_run_id) if r["is_centroid"]]

    # --- writing a set (via MolSuite/scopes, not raw DB) ----------------------
    def save_selection_as_set(self, molecule_ids: Iterable[int], *, name: str) -> MoleculeSetRef:
        """Persist the chosen representatives as a molecule set (an enrichment set) so the rest of
        the app (docking, filter, export) can target the redundancy-reduced library."""
        self.runtime._require_active_project()
        ids = [int(i) for i in molecule_ids]
        if not ids:
            raise ValueError("No molecules to save as a set.")
        ref = create_snapshot_set(
            self.runtime.molsuite.project_db,
            entity_kind="molecule",
            name=str(name).strip() or "diverse_selection",
            entity_ids=ids,
            kind=SetPurpose.ENRICHMENT,
        )
        return MoleculeSetRef(id=int(ref.id), kind="snapshot")

    def molblock_for(self, molecule_id: int) -> str | None:
        """A 2-D-drawable molblock for one molecule (its stored structure file), or None if there's
        no usable structure. Best-effort — used by the universe scatter's hover preview."""
        from rdkit import Chem

        from amdockvs.molecule_paths import preferred_molecule_path

        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rec = session.get(MoleculeRecord, int(molecule_id))
            path = preferred_molecule_path(rec) if rec is not None else None
        if path is None or not path.exists():
            return None
        suffix = path.suffix.lower()
        if suffix in {".sdf", ".mol"}:
            mol = next(iter(Chem.SDMolSupplier(str(path), sanitize=True, removeHs=True)), None)
        elif suffix == ".mol2":
            mol = Chem.MolFromMol2File(str(path), sanitize=True, removeHs=True)
        elif suffix in {".pdb", ".ent"}:
            mol = Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=True)
        else:
            mol = None
        return Chem.MolToMolBlock(mol) if mol is not None else None

    # --- saved results: durable summary row + parquet graph sidecar -----------
    def _project_root(self) -> Path:
        db_path = getattr(self.runtime.molsuite.project_db, "db_path", None)
        if not db_path:
            raise ValueError("No project DB path — cannot resolve the run sidecar location.")
        return Path(db_path).expanduser().resolve().parent

    def save_clustering_result(
        self,
        *,
        points: Iterable[tuple],
        cluster_stats: list[dict[str, Any]],
        evr: Iterable[float],
        method: str,
        threshold: float,
        scope_label: str,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        n_molecules: int | None = None,
        n_clusters: int | None = None,
        n_reps: int | None = None,
    ) -> str:
        """Persist an applied clustering as a durable run: the graph points (x, y, molecule_id,
        cluster_id, is_centroid) to a parquet sidecar, and a compact ``ClusteringRun`` row (metadata
        + counts + the sidecar path). Returns the run id. Only the PCA + cluster flags are stored —
        fingerprints stay in their table, loaded by id if ever needed. Counts default to what the
        points imply; the parallel path passes them explicitly (it saves no graph sidecar yet)."""
        import pandas as pd

        self.runtime._require_active_project()
        run_id = uuid.uuid4().hex
        snap_dir = self._project_root() / "clustering_runs"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot = snap_dir / f"{run_id}.parquet"
        frame = pd.DataFrame(
            list(points), columns=["x", "y", "molecule_id", "cluster_id", "is_centroid"]
        )
        frame.to_parquet(snapshot, index=False)
        row = ClusteringRun(
            run_id=run_id, method=str(method), threshold=float(threshold), scope_label=str(scope_label),
            fp_radius=int(fp_radius), fp_nbits=int(fp_nbits),
            n_molecules=int(n_molecules if n_molecules is not None else len(frame)),
            n_clusters=int(n_clusters if n_clusters is not None else len(cluster_stats or [])),
            n_reps=int(n_reps if n_reps is not None else (frame["is_centroid"].sum() if len(frame) else 0)),
            snapshot_path=str(snapshot), evr=list(evr or [0.0, 0.0]), cluster_stats=list(cluster_stats or []),
        )
        with self.runtime.molsuite.project_db.get_session() as session:
            session.add(row)
            session.commit()
        return run_id

    def list_clustering_results(self) -> list[dict[str, Any]]:
        """Saved runs, newest first — for the results table."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(select(ClusteringRun).order_by(ClusteringRun.id.desc())).all()
        return [
            {"run_id": r.run_id, "created_at": r.created_at, "method": r.method, "threshold": r.threshold,
             "scope_label": r.scope_label, "n_molecules": r.n_molecules, "n_clusters": r.n_clusters,
             "n_reps": r.n_reps}
            for r in rows
        ]

    def load_clustering_result(self, run_id: str) -> dict[str, Any]:
        """Everything needed to redraw a saved run (graph points + cluster stats + evr) — read the
        sidecar directly, no re-clustering or PCA recompute."""
        import pandas as pd

        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            row = session.exec(select(ClusteringRun).where(ClusteringRun.run_id == str(run_id))).first()
        if row is None:
            raise ValueError(f"Unknown clustering run: {run_id}")
        if row.snapshot_path and Path(row.snapshot_path).exists():
            frame = pd.read_parquet(row.snapshot_path)
        else:
            frame = pd.DataFrame(columns=["x", "y", "molecule_id", "cluster_id", "is_centroid"])
        return {
            "run_id": row.run_id, "method": row.method, "threshold": row.threshold,
            "scope_label": row.scope_label, "n_molecules": row.n_molecules, "n_clusters": row.n_clusters,
            "n_reps": row.n_reps, "evr": list(row.evr or [0.0, 0.0]),
            "cluster_stats": list(row.cluster_stats or []), "points": frame.to_dict("records"),
        }

    def delete_clustering_result(self, run_id: str) -> None:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            row = session.exec(select(ClusteringRun).where(ClusteringRun.run_id == str(run_id))).first()
            if row is None:
                return
            snapshot = row.snapshot_path
            session.delete(row)
            session.commit()
        if snapshot:
            Path(snapshot).unlink(missing_ok=True)

    def save_centroids_as_set(self, cluster_run_id: str, *, name: str = "") -> MoleculeSetRef:
        return self.save_selection_as_set(
            self.centroid_ids(cluster_run_id),
            name=name or f"centroids_{cluster_run_id[:8]}",
        )


__all__ = ["AnalysisResult", "SelectionAPI"]
