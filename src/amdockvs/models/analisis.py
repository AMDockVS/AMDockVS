from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, UniqueConstraint, Index
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_CLUSTERING_RESULTS,
    TABLE_MOLECULES,
    TABLE_SIMILARITY_RESULTS,
)
# Centralised vocabulary - re-exported so existing imports keep working.
from amdockvs.vocab import ClusteringMethod, SimilarityMethod


# ---------------------------------------------------------------------------
# SimilarityResult
# ---------------------------------------------------------------------------

class SimilarityResult(SQLModel, table=True):
    """
    Pairwise similarity score between two molecules.
    query_id is the reference molecule; target_id is compared against it.

    fp_type records which fingerprint was used for the comparison.
    Scores range 0.0–1.0 (method-dependent).

    Results are directional by convention (query → target) but similarity
    is symmetric — only one direction is stored to avoid duplication.
    The application layer ensures query_id < target_id if needed.
    """

    __tablename__ = TABLE_SIMILARITY_RESULTS
    __table_args__ = (
        UniqueConstraint("query_id", "target_id", "fp_type", "method"),
        Index("idx_sim_query", "query_id"),
        Index("idx_sim_target", "target_id"),
        Index("idx_sim_score", "query_id", "score"),
    )

    id: int | None = Field(default=None, primary_key=True)
    query_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    target_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    fp_type: str = Field()  # fingerprint used
    method: str = Field(default=SimilarityMethod.TANIMOTO)
    score: float = Field()  # 0.0 – 1.0
    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_rows(
            cls,
            query_id: int,
            targets: list[dict],  # [{"target_id": int, "score": float}]
            fp_type: str,
            method: str = SimilarityMethod.TANIMOTO,
    ) -> list[dict[str, Any]]:
        now = datetime.now()
        return [
            {
                "query_id": query_id,
                "target_id": t["target_id"],
                "fp_type": fp_type,
                "method": method,
                "score": t["score"],
                "created_at": now,
            }
            for t in targets
        ]


# ---------------------------------------------------------------------------
# ClusteringResult
# ---------------------------------------------------------------------------

class ClusteringResult(SQLModel, table=True):
    """
    Cluster assignment for a molecule from a specific clustering run.
    One row per (molecule, run).

    cluster_run_id groups all assignments from the same clustering job —
    use a UUID or timestamp string generated at job start.

    is_centroid=True marks the representative molecule of each cluster.
    Centroids are the primary candidates after cluster-based filtering.
    """

    __tablename__ = TABLE_CLUSTERING_RESULTS
    __table_args__ = (
        UniqueConstraint("molecule_id", "cluster_run_id"),
        Index("idx_cluster_run", "cluster_run_id"),
        Index("idx_cluster_centroid", "cluster_run_id",
              sqlite_where=__import__('sqlalchemy').text("is_centroid=1")),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    cluster_run_id: str = Field(index=True)  # UUID identifying the run
    cluster_id: int = Field()  # cluster label (0-based)
    is_centroid: bool = Field(default=False)
    method: str = Field(default=ClusteringMethod.BUTINA)
    fp_type: str = Field(default="")  # fingerprint used
    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_rows(
            cls,
            cluster_run_id: str,
            assignments: list[dict],  # [{"molecule_id": int, "cluster_id": int, "is_centroid": bool}]
            method: str = ClusteringMethod.BUTINA,
            fp_type: str = "",
    ) -> list[dict[str, Any]]:
        now = datetime.now()
        return [
            {
                "molecule_id": a["molecule_id"],
                "cluster_run_id": cluster_run_id,
                "cluster_id": a["cluster_id"],
                "is_centroid": a.get("is_centroid", False),
                "method": method,
                "fp_type": fp_type,
                "created_at": now,
            }
            for a in assignments
        ]


class ClusteringRun(SQLModel, table=True):
    """Compact summary of a saved (applied) clustering run — the durable traceability record.

    The per-point graph data (2-D PCA coords + cluster/centroid flags) lives in a parquet sidecar at
    ``snapshot_path`` — offloaded like the SDF-tag sidecars, NOT kept in the DB. This row is the
    index: metadata + counts + the sidecar reference. Fingerprints stay in the fingerprints table,
    loaded by id only when needed. Viewing a run reads the sidecar directly (no re-clustering, no PCA
    recompute) so it scales regardless of library size.
    """

    __tablename__ = "clustering_runs"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)  # uuid
    created_at: datetime = Field(default_factory=datetime.now)
    method: str = Field(default="")
    threshold: float = Field(default=0.0)
    scope_label: str = Field(default="")     # e.g. "General", "Reference", "Set #3"
    fp_radius: int = Field(default=2)
    fp_nbits: int = Field(default=2048)
    n_molecules: int = Field(default=0)
    n_clusters: int = Field(default=0)
    n_reps: int = Field(default=0)
    snapshot_path: str = Field(default="")   # parquet sidecar: x, y, molecule_id, cluster_id, is_centroid
    evr: list = Field(default_factory=list, sa_type=JSON)           # [pc1_var, pc2_var] for axis labels
    cluster_stats: list = Field(default_factory=list, sa_type=JSON)  # [{cluster_id, size, tightness}]
