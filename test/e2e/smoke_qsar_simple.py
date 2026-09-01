"""End-to-end smoke for the simple-QSAR path (run directly, not under pytest's import machinery):

  import a SMILES+IC50 file (activity-at-import) -> activities present
  -> compute_descriptors(compute_fingerprints=True) -> FingerprintRecord rows
  -> train(feature_source=ecfp4, split=scaffold, cv_folds=3) -> model with metrics
  -> predict -> predictions written.

Process-pool jobs need the __main__ guard; prints are flushed so os._exit can't eat them.
"""
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    from amdockvs import AMDockVSRuntime
    from amdockvs.models import ActivityRecord, FingerprintRecord, QSARPredictionRecord
    from sqlmodel import select

    project_dir = Path(tempfile.mkdtemp(prefix="qsar_smoke_")) / "proj"
    # 8 distinct scaffolds so a scaffold split is meaningful.
    smiles = [
        ("CCO", "ethanol", 100), ("CCCO", "propanol", 50), ("c1ccccc1", "benzene", 200),
        ("c1ccncc1", "pyridine", 75), ("CC(=O)O", "acetic", 300), ("CCN", "ethylamine", 25),
        ("c1ccc2ccccc2c1", "naphthalene", 10), ("C1CCCCC1", "cyclohexane", 500),
    ]
    lig_file = project_dir.parent / "ligs.smi"
    project_dir.mkdir(parents=True, exist_ok=True)
    lig_file.write_text("SMILES,Name,IC50\n" + "\n".join(f"{s},{n},{v}" for s, n, v in smiles) + "\n")

    runtime = AMDockVSRuntime()
    try:
        runtime.create_or_open_project(name=f"qsar_smoke_{os.getpid()}", folder=project_dir)
        prefilter = {
            "activity_property": "IC50", "activity_endpoint": "pIC50",
            "activity_unit": "nM", "activity_transform": "pIC50",
        }
        jobs = runtime.loader.load_ligands([lig_file], executor_name="thread", prefilter=prefilter)
        runtime.wait_for_jobs(list(jobs))

        with runtime.molsuite.project_db.get_session() as session:
            acts = session.exec(select(ActivityRecord)).all()
        print(f"[1] activities at import: {len(acts)} (pIC50 = {sorted(round(a.value,2) for a in acts)})", flush=True)
        assert len(acts) == len(smiles), f"expected {len(smiles)} activities, got {len(acts)}"

        fp_job = runtime.qsar.compute_fingerprints(only_missing=True, executor_name="compute")
        runtime.wait_for_job(fp_job)
        with runtime.molsuite.project_db.get_session() as session:
            fps = session.exec(select(FingerprintRecord)).all()
        print(f"[2] fingerprint rows: {len(fps)} (nbits={fps[0].nbits if fps else '-'})", flush=True)
        assert len(fps) == len(smiles), f"expected {len(smiles)} fingerprints, got {len(fps)}"

        model = runtime.qsar.train(
            endpoint="pIC50", feature_source="ecfp4", algorithm="random_forest",
            split="scaffold", test_size=0.3, cv_folds=3,
        )
        m = model.metrics or {}
        print(f"[3] trained model #{model.id} feature_kind={m.get('feature_kind')} "
              f"train={m.get('train')} test={m.get('test')} q2={m.get('q2')}", flush=True)
        assert m.get("feature_kind") == "ecfp4"

        result = runtime.qsar.predict(model=int(model.id))
        with runtime.molsuite.project_db.get_session() as session:
            preds = session.exec(select(QSARPredictionRecord)).all()
        print(f"[4] predicted: {result['predicted']}, prediction rows: {len(preds)}", flush=True)
        assert result["predicted"] == len(smiles)

        print("SMOKE OK", flush=True)
        return 0
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    sys.exit(main())
