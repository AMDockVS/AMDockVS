"""Whole-product smoke: import -> tools -> preparation -> docking -> analysis, plus the htpvs twin.

Run directly (process pools need a __main__ guard), not under pytest:

    python test/e2e/smoke_full_pipeline.py

Every stage is isolated: one failure is recorded and the run continues, so a single pass says
which parts of the product work and which do not. Exit code is the number of failed stages.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DATA = REPO / "data" / "docking"
RECEPTOR = DATA / "1iep_receptorH.pdb"
LIGANDS = DATA / "ligs.sdf"
BOX = DATA / "1iep_receptor.box.txt"
EXECUTOR = "thread"

RESULTS: list[tuple[str, str, str]] = []


def stage(name):
    """Decorator-free stage runner: records PASS/FAIL/SKIP and never raises."""
    def run(fn):
        start = time.monotonic()
        try:
            note = fn()
            status = "SKIP" if isinstance(note, str) and note.startswith("skip:") else "PASS"
            RESULTS.append((status, name, f"{note or ''} [{time.monotonic() - start:.1f}s]"))
            return note
        except Exception as error:  # noqa: BLE001 - the point is to keep going
            RESULTS.append(("FAIL", name, f"{type(error).__name__}: {error}"))
            traceback.print_exc()
            return None
    return run


def _why(row) -> str:
    """JobStatus has no `error` field: the counters are what says a job really worked."""
    return (f"status={row.status} chunks done={row.chunks_done} failed={row.chunks_failed} "
            f"stage_failed={row.chunks_stage_failed} total={row.chunks_total}")


def parse_box():
    values = {}
    for line in BOX.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = float(value)
    return (
        (values["center_x"], values["center_y"], values["center_z"]),
        (values["size_x"], values["size_y"], values["size_z"]),
    )


SMILES = [
    ("CCCCCCO", "hexanol", 5.1), ("CCCCCCCO", "heptanol", 5.4), ("CCCCCCCCO", "octanol", 5.8),
    ("c1ccccc1C", "toluene", 4.2), ("c1ccccc1CC", "ethylbenzene", 4.5),
    ("c1ccccc1CCC", "propylbenzene", 4.9), ("C1CCCCC1", "cyclohexane", 3.8),
    ("c1ccncc1", "pyridine", 3.2), ("c1ccc2ccccc2c1", "naphthalene", 5.0),
    ("CC(=O)Nc1ccccc1", "acetanilide", 4.1), ("CC(=O)Oc1ccccc1C(=O)O", "aspirin", 4.4),
    ("CN1C=NC2=C1C(=O)N(C)C(=O)N2C", "caffeine", 3.9), ("OCC1OC(O)C(O)C(O)C1O", "glucose", 2.1),
    ("CCN(CC)CC", "triethylamine", 3.4), ("c1ccc(cc1)S(=O)(=O)N", "benzenesulfonamide", 3.6),
    ("CCOC(=O)c1ccccc1", "ethylbenzoate", 4.6),
]


def vs_project(rt, tmp):
    ligand_smi = tmp / "extra.smi"
    ligand_smi.write_text("SMILES,Name\n" + "\n".join(f"{s},{n}" for s, n, _ in SMILES) + "\n")

    # ---------------------------------------------------------------- import
    @stage("import receptor + SDF ligands + SMILES ligands")
    def _import():
        jobs = [
            *rt.loader.load_receptors([RECEPTOR], executor_name=EXECUTOR),
            *rt.loader.load_ligands([LIGANDS], executor_name=EXECUTOR),
        ]
        final = rt.wait_for_jobs(jobs, timeout_s=900, poll_s=0.3)
        assert all(row.status == "completed" for row in final.values()), final
        docking_ids = [int(m.id) for m in rt.molecules.stream(rt.molecules.select(role="ligand"))]
        extra = rt.wait_for_jobs(rt.loader.load_ligands([ligand_smi], executor_name=EXECUTOR),
                                 timeout_s=900, poll_s=0.3)
        assert all(row.status == "completed" for row in extra.values()), extra
        return f"{len(docking_ids)} sdf + {len(SMILES)} smi ligands, 1 receptor"

    lig_all = rt.molecules.select(role="ligand", excluded=False)
    rec_scope = rt.molecules.select(role="receptor", excluded=False)
    sdf_ids = [int(m.id) for m in rt.molecules.stream(lig_all)][:4]
    dock_set = rt.molecules.create_set(sdf_ids, name="dock_set")
    receptor_id = int(next(iter(rt.molecules.stream(rec_scope))).id)

    # ----------------------------------------------------------- tools: chemistry
    @stage("tool: standardize ligands")
    def _standardize():
        job = rt.chemistry.standardize_ligands(ligands=lig_all, batch_size=16, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("tool: protonate ligands")
    def _protonate():
        job = rt.chemistry.protonate_ligands(ligands=lig_all, batch_size=16, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("tool: generate 3D")
    def _gen3d():
        job = rt.chemistry.generate_3d_ligands(ligands=lig_all, batch_size=16, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        assert rt.molecules.count(rt.molecules.select(role="ligand", has_3d=True)) > 0
        return f"{rt.molecules.count(rt.molecules.select(role='ligand', has_3d=True))} with 3D"

    @stage("tool: minimize ligands")
    def _minimize():
        job = rt.chemistry.minimize_ligands(ligands=lig_all, batch_size=16, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("tool: conformer ensemble")
    def _conformers():
        job = rt.chemistry.generate_ligand_conformers(
            ligands=rt.molecules.create_set(sdf_ids[:2], name="conf_set"),
            batch_size=4, executor_name=EXECUTOR,
        )
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("tool: protonate receptor")
    def _receptor_chem():
        import shutil
        # `reduce` is an external binary; pdb2pqr is a python package. Use whichever is here,
        # so a missing optional tool reads as one skipped backend, not a broken step.
        method = "reduce" if shutil.which("reduce") else "pdb2pqr"
        job = rt.chemistry.protonate_receptors(receptors=rec_scope, batch_size=4,
                                               method=method, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", f"method={method} {_why(row)}"
        return f"method={method}"

    # ------------------------------------------------------- tools: binding sites
    @stage("tool: binding site from box file + active site")
    def _sites():
        center, size = parse_box()
        site = rt.binding_sites.save_site(molecule_id=receptor_id, name="box", center=center,
                                          size=size, set_active=True)
        assert rt.binding_sites.list_sites(molecule_id=receptor_id)
        return f"site {site.id}"

    @stage("tool: auto box from reference ligand (Rg)")
    def _autobox():
        box = rt.binding_sites.suggest_box_from_ligand(ligand_id=sdf_ids[0])
        assert box["size"][0] > 0, box
        return f"rg={box.get('rg'):.2f}"

    @stage("tool: p2rank pocket prediction")
    def _p2rank():
        status = rt.binding_sites.tool_status()
        if not status.installed:
            return "skip: p2rank not installed"
        job = rt.binding_sites.predict(receptor_ids=[receptor_id], executor_name="compute")
        row = rt.wait_for_job(job, poll_s=0.5)
        assert row.status == "completed", _why(row)
        return f"{len(rt.binding_sites.list_sites(molecule_id=receptor_id))} sites"

    # ------------------------------------------------------------ tools: qsar
    @stage("tool: descriptors + fingerprints")
    def _descriptors():
        job = rt.qsar.compute_descriptors(only_missing=False, compute_fingerprints=True,
                                          executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        assert rt.qsar.list_descriptors()
        return f"{len(rt.qsar.list_descriptors())} descriptor columns"

    @stage("tool: activities + train + predict + evaluate")
    def _qsar():
        by_name = {str(m.name): int(m.id) for m in rt.molecules.stream(lig_all)}
        labeled = [by_name[name] for _s, name, _v in SMILES if name in by_name]
        for _s, name, value in SMILES:
            if name in by_name:
                rt.qsar.set_activity(ligand_id=by_name[name], endpoint="logP", value=value)
        assert rt.qsar.list_endpoints() == ["logP"], rt.qsar.list_endpoints()
        qsar_set = rt.molecules.create_set(labeled, name="qsar_set")
        model = rt.qsar.train(endpoint="logP", molecule_set=qsar_set, algorithm="random_forest",
                              task="regression", feature_source="descriptors", cv_folds=3, seed=1)
        predicted = rt.qsar.predict(model=model, molecule_set=qsar_set)
        scored = rt.qsar.evaluate(model=model, molecule_set=qsar_set, endpoint="logP")
        return f"model {model.id}, predicted {predicted.get('predicted')}, r2={scored.get('r2'):.2f}"

    # ------------------------------------------------------- tools: diversity
    @stage("tool: diversity clustering + centroids set")
    def _diversity():
        run_id = "smoke_cluster"
        job = rt.diversity.cluster_job(method="bitbirch_lean", threshold=0.5, per_cluster=1,
                                       cluster_run_id=run_id, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        centroids = rt.diversity.centroid_ids(run_id)
        assert centroids, "clustering produced no centroids"
        ref = rt.diversity.save_centroids_as_set(run_id, name="centroids")
        return f"{len(centroids)} centroids -> set {ref.id}"

    # -------------------------------------------------------------- preparation
    @stage("preparation: ligands + receptors (PDBQT)")
    def _prepare():
        jobs = [
            rt.docking.prepare_ligands(ligand_set=dock_set, batch_size=8, executor_name=EXECUTOR),
            rt.docking.prepare_receptors(receptor_set=rec_scope, batch_size=4, executor_name=EXECUTOR),
        ]
        final = rt.wait_for_jobs(jobs, timeout_s=1800, poll_s=0.3)
        assert all(row.status == "completed" for row in final.values()), final
        ready = rt.docking.check_required(ligand_set=dock_set, receptor_set=rec_scope)
        assert ready.get("ready"), ready
        return "ready"

    # ------------------------------------------------------------------ docking
    @stage("docking: set_grid + run")
    def _dock():
        center, size = parse_box()
        rt.docking.set_grid(receptor_id=receptor_id, center=center, size=size)
        assert rt.docking.get_grid(receptor_id=receptor_id)
        job = rt.docking.run(ligand_set=dock_set, receptor_set=rec_scope, batch_size=4,
                             exhaustiveness=4, num_modes=5, vina_backend="binary", vina_cpu=2,
                             executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.5)
        assert row.status == "completed", _why(row)
        stats = rt.docking.result_stats()
        assert stats.total_results > 0, stats
        return f"{stats.total_results} results, best={stats.best_score}"

    # ----------------------------------------------------------------- analysis
    @stage("analysis: stats, top hits, summaries, filters")
    def _analysis():
        hits = rt.docking.top_hits(limit=5)
        assert hits, "no hits"
        assert rt.docking.ligand_summaries()
        assert rt.docking.receptor_summaries()
        rt.docking.filtered_hits(limit=5)
        rt.docking.list_results(limit=10)
        rt.docking.result_protocols()
        rt.docking.pivot_availability()
        best = hits[0]
        return f"best {best.score} ({best.ligand_name or best.ligand_id})"

    @stage("analysis: interactions")
    def _interactions():
        job = rt.docking.compute_interactions(pose_rank=1, executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        stats = rt.docking.interaction_stats()
        return f"{stats}"[:120]

    @stage("analysis: 2D diagrams")
    def _diagrams():
        job = rt.docking.compute_diagrams(pose_rank=1, fmt="png", executor_name=EXECUTOR)
        row = rt.wait_for_job(job, poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("analysis: complexes")
    def _complexes():
        ref = rt.complexes.create(receptor_molecule_id=receptor_id, ligand_molecule_id=sdf_ids[0],
                                  name="smoke_complex", purpose="redocking")
        assert rt.complexes.get(ref) is not None
        assert rt.complexes.count() >= 1
        return f"complex {ref.id}"


def htpvs_project(rt, tmp):
    """The sharded twin: import -> chemistry over shards (generations) -> prepare -> dock."""
    lib = tmp / "htp_lib.smi"
    lib.write_text("SMILES,Name\n" + "\n".join(f"{s},{n}" for s, n, _ in SMILES) + "\n")

    @stage("htpvs: shard import")
    def _shard_import():
        from amdockvs.molecules.storage import ShardStore, shard_scope_spec
        jobs = rt.loader.shard_ligands([lib], shard_size=6, executor_name=EXECUTOR)
        final = rt.wait_for_jobs(jobs, timeout_s=600, poll_s=0.3)
        assert all(row.status == "completed" for row in final.values()), final
        store = ShardStore(rt.molsuite.project_db)
        whole = shard_scope_spec(state=None)
        assert store.record_count(whole) == len(SMILES), store.record_count(whole)
        return f"{store.count(whole)} shards / {store.record_count(whole)} records, mode={rt.mode}"

    @stage("htpvs: chemistry over shards (generation switch)")
    def _shard_chem():
        from amdockvs.molecules.storage import (
            ShardStore, active_generation_id, shard_scope_spec,
        )
        db = rt.molsuite.project_db
        whole = shard_scope_spec(state=None)
        before = active_generation_id(db)
        row = rt.chemistry.run_shard_pipeline(["standardize"], executor_name=EXECUTOR, wait=True)
        assert row.status == "completed", _why(row)
        mid = active_generation_id(db)
        assert mid != before, "the generation did not switch"
        # A second step must find the library the first one wrote: chaining is the whole point.
        row = rt.chemistry.run_shard_pipeline(["generate_3d"], executor_name=EXECUTOR, wait=True)
        assert row.status == "completed", _why(row)
        after = active_generation_id(db)
        assert after != mid, "the second generation did not switch"
        store = ShardStore(db)
        assert store.record_count(whole) == len(SMILES), store.record_count(whole)
        return f"gen {before} -> {mid} -> {after}, {store.record_count(whole)} records survive"

    @stage("htpvs: prepare shards (PDBQT)")
    def _shard_prepare():
        row = rt.wait_for_job(rt.docking.prepare_ligand_shards(executor_name=EXECUTOR), poll_s=0.3)
        assert row.status == "completed", _why(row)
        return row.status

    @stage("htpvs: dock shards with a hit gate")
    def _shard_dock():
        jobs = rt.loader.load_receptors([RECEPTOR], executor_name=EXECUTOR)
        assert all(r.status == "completed" for r in rt.wait_for_jobs(jobs, timeout_s=600, poll_s=0.3).values())
        rec_scope = rt.molecules.select(role="receptor", excluded=False)
        receptor_id = int(next(iter(rt.molecules.stream(rec_scope))).id)
        prep = rt.wait_for_job(rt.docking.prepare_receptors(receptor_set=rec_scope, batch_size=2,
                                                            executor_name=EXECUTOR), poll_s=0.3)
        assert prep.status == "completed", _why(prep)
        center, size = parse_box()
        rt.docking.set_grid(receptor_id=receptor_id, center=center, size=size)
        row = rt.wait_for_job(
            rt.docking.run_shards(receptor_set=rec_scope, hit_threshold=-4.0, hit_cap=5,
                                  exhaustiveness=4, num_modes=3, vina_cpu=2,
                                  executor_name=EXECUTOR),
            poll_s=0.5,
        )
        assert row.status == "completed", _why(row)
        return f"{rt.docking.result_stats()}"[:120]


def main() -> int:
    from amdockvs import AMDockVSRuntime

    tmp = Path(tempfile.mkdtemp(prefix="amdock_smoke_"))
    for label, folder, body in (("VS", "vs_project", vs_project), ("HTPVS", "htp_project", htpvs_project)):
        rt = AMDockVSRuntime()
        try:
            rt.create_project(name=f"smoke_{folder}", folder=tmp / folder)
            print(f"\n######## {label} ########", flush=True)
            body(rt, tmp)
        except Exception as error:  # noqa: BLE001
            RESULTS.append(("FAIL", f"{label} project setup", f"{type(error).__name__}: {error}"))
            traceback.print_exc()
        finally:
            try:
                rt.shutdown()
            except Exception:
                pass

    print("\n================ SMOKE REPORT ================", flush=True)
    for status, name, note in RESULTS:
        print(f"{status:4}  {name:52}  {note}", flush=True)
    failed = sum(1 for status, _n, _d in RESULTS if status == "FAIL")
    print(f"\n{len(RESULTS)} stages: "
          f"{sum(1 for s, _, _ in RESULTS if s == 'PASS')} pass, "
          f"{sum(1 for s, _, _ in RESULTS if s == 'SKIP')} skip, {failed} fail", flush=True)
    print(f"workdir: {tmp}", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(main())
