"""End-to-end smoke for diversity selection (run directly, not under pytest import machinery):

  import SMILES ligands -> compute fingerprints -> cluster_job (durable, over the DB)
  -> clustering_results rows written -> analyze() (inline) returns clusters + projection
  -> save centroids as a molecule set.

Process-pool jobs need the __main__ guard; prints are flushed so os._exit can't eat them.
"""
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    from amdockvs import AMDockVSRuntime
    from amdockvs.models import ClusteringResult
    from amdockvs.scopes import list_molecule_set_ids
    from sqlmodel import select

    project_dir = Path(tempfile.mkdtemp(prefix="diversity_smoke_")) / "proj"
    # Two families of near-duplicates + a couple of singletons -> expect real redundancy reduction.
    smiles = [
        ("CCCCCCO", "hexanol"), ("CCCCCCCO", "heptanol"), ("CCCCCCCCO", "octanol"),
        ("c1ccccc1C", "toluene"), ("c1ccccc1CC", "ethylbenzene"), ("c1ccccc1CCC", "propylbenzene"),
        ("C1CCCCC1", "cyclohexane"), ("c1ccncc1", "pyridine"),
    ]
    lig_file = project_dir.parent / "ligs.smi"
    project_dir.mkdir(parents=True, exist_ok=True)
    lig_file.write_text("SMILES,Name\n" + "\n".join(f"{s},{n}" for s, n in smiles) + "\n")

    runtime = AMDockVSRuntime()
    try:
        runtime.create_or_open_project(name=f"diversity_smoke_{os.getpid()}", folder=project_dir)
        jobs = runtime.loader.load_ligands([lig_file], executor_name="thread")
        runtime.wait_for_jobs(list(jobs))

        fp_job = runtime.qsar.compute_fingerprints(only_missing=True, executor_name="compute")
        runtime.wait_for_job(fp_job)

        # [1] durable DB-async clustering job -> clustering_results rows
        run_id = "smoke_run"
        job_id = runtime.selection.cluster_job(
            method="bitbirch_lean", threshold=0.45, per_cluster=1,
            cluster_run_id=run_id, executor_name="compute",
        )
        runtime.wait_for_job(job_id)
        with runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(
                select(ClusteringResult).where(ClusteringResult.cluster_run_id == run_id)
            ).all()
        n_clusters = len({r.cluster_id for r in rows})
        n_centroids = sum(1 for r in rows if r.is_centroid)
        print(f"[1] clustering_results: {len(rows)} rows, {n_clusters} clusters, {n_centroids} centroids", flush=True)
        assert len(rows) == len(smiles), f"expected {len(smiles)} assignments, got {len(rows)}"
        assert n_centroids == n_clusters, "one centroid per cluster expected at per_cluster=1"
        assert 1 < n_clusters < len(smiles), f"expected real reduction, got {n_clusters} clusters"
        runtime.selection.register_run_from_sidecar(
            run_id, method="bitbirch_lean", threshold=0.45, scope_label="smoke", fp_radius=2, fp_nbits=2048
        )
        saved = runtime.selection.load_clustering_result(run_id)
        print(f"[1b] saved result: {len(saved['points'])} points, {saved['n_clusters']} clusters", flush=True)
        assert len(saved["points"]) == len(smiles)
        assert saved["n_clusters"] == n_clusters

        # [2] inline analyze -> stats + projection for the UI
        analysis = runtime.selection.analyze(method="bitbirch_lean", threshold=0.45)
        print(f"[2] analyze: {analysis.stats['n_clusters']} clusters, "
              f"{len(analysis.representative_ids)} reps, projection={len(analysis.projection)} pts", flush=True)
        assert len(analysis.projection) == len(analysis.molecule_ids) == len(smiles)
        assert len(analysis.representative_ids) == analysis.stats["n_clusters"]

        # [2b] bounded preview: sample_limit caps inline work regardless of library size
        sampled = runtime.selection.analyze(method="bitbirch_lean", threshold=0.5, sample_limit=4)
        print(f"[2b] sampled preview: sampled={sampled.sampled} shown={len(sampled.molecule_ids)} "
              f"total={sampled.total_in_scope}", flush=True)
        assert sampled.sampled is True and len(sampled.molecule_ids) == 4 and sampled.total_in_scope == len(smiles)

        # [3] persist the reduced library as a set (via scopes, not raw DB)
        ref = runtime.selection.save_centroids_as_set(run_id, name="smoke_centroids")
        members = list_molecule_set_ids(runtime.molsuite.project_db, int(ref.id))
        print(f"[3] centroid set #{ref.id}: {len(members)} members", flush=True)
        assert len(members) == n_centroids

        print("SMOKE OK", flush=True)
        return 0
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    sys.exit(main())
