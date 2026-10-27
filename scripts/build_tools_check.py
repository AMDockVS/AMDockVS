"""Headless check for every op the Build tool (Molecule Build) exposes.

Runs each runtime.chemistry.* call the Build panel wires to its buttons, on the same
kind of scope the panel builds (molecule_type + structure_source), and reports
PASS/FAIL per op with the real chunk error out of executor.db.

    python scripts/build_tools_check.py [op ...]
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

from amdockvs.runtime import AMDockVSRuntime
from amdockvs.core.vocab import MoleculeType

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "docking"
PROJECTS_ROOT = REPO / "_tmp_build_tools_check"


def chunk_errors(rt) -> list[str]:
    db = Path(rt.active_context.path) / "executor.db"
    if not db.exists():
        return []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        rows = con.execute(
            "SELECT error FROM executor_job_chunks WHERE error IS NOT NULL AND error != ''"
        ).fetchall()
    return [str(row[0]) for row in rows]


def run_op(rt, name: str, call, *, seen_errors: set[str]) -> bool:
    started = time.monotonic()
    try:
        job_id = call()
        status = rt.wait_for_job(job_id, poll_s=0.5)
    except Exception as exc:  # submit-time rejection is a result too
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        return False
    # ponytail: job status lies about chunk failures, so diff executor.db errors per op
    new_errors = [err for err in chunk_errors(rt) if err not in seen_errors]
    seen_errors.update(new_errors)
    ok = status.status == "completed" and not status.chunks_failed and not new_errors
    print(
        f"{'PASS' if ok else 'FAIL'} {name}: status={status.status} "
        f"chunks={status.chunks_done}/{status.chunks_total} failed={status.chunks_failed} "
        f"({time.monotonic() - started:.1f}s)"
    )
    for err in new_errors[:2]:
        print("     " + err.strip().splitlines()[-1])
    return ok


def main() -> int:
    wanted = set(sys.argv[1:])
    # ponytail: a fresh dir per run — the project registry refuses a reused path
    shutil.rmtree(PROJECTS_ROOT, ignore_errors=True)
    project_dir = PROJECTS_ROOT / f"run_{int(time.time())}"
    rt = AMDockVSRuntime()
    rt.create_project(name=project_dir.name, folder=project_dir, description="Build tool ops check")
    seen: set[str] = set()
    results: dict[str, bool] = {}
    try:
        jobs = [
            *rt.loader.load_ligands([DATA / "ligs.sdf"], executor_name="compute"),
            *rt.loader.load_receptors([DATA / "1iep_receptorH.pdb"], executor_name="compute"),
        ]
        rt.wait_for_jobs(jobs, timeout_s=600.0)
        print(f"imported: {rt.molecules.count()} molecules")

        ligands = rt.molecules.select(molecule_type=MoleculeType.SMALL_MOLECULE)
        receptors = rt.molecules.select(molecule_type=MoleculeType.PROTEIN)
        chem = rt.chemistry

        for name, status in (("openbabel", None), ("pkasso", None)):
            info = chem.small_molecule_protonation_tool_status(name)
            print(f"runtime {name}: installed={info.installed} — {info.message}")

        ops = {
            # small molecules
            "protonate_dimorphite": lambda: chem.protonate_ligands(
                ligands=ligands, method="dimorphite", ph=7.4, batch_size=128),
            "protonate_openbabel": lambda: chem.protonate_ligands(
                ligands=ligands, method="openbabel", ph=7.4, batch_size=128),
            "protonate_polar_hs": lambda: chem.protonate_ligands(
                ligands=ligands, method="polar_hydrogens", batch_size=128),
            "generate_3d": lambda: chem.generate_3d_ligands(
                ligands=ligands, add_hs=True, optimize=True, batch_size=128),
            "conformers": lambda: chem.generate_ligand_conformers(
                ligands=ligands, num_conformers=5, prune_rms_thresh=0.5, batch_size=128),
            "minimize_ligands": lambda: chem.minimize_ligands(
                ligands=ligands, forcefield="mmff", max_iters=200, batch_size=128),
            # proteins
            "protonate_receptors": lambda: chem.protonate_receptors(
                receptors=receptors, method="reduce", batch_size=8),
            "protonate_receptors_pdb2pqr": lambda: chem.protonate_receptors(
                receptors=receptors, method="pdb2pqr", ph=7.0, forcefield="AMBER", batch_size=8),
            "fix_receptors": lambda: chem.fix_receptors(
                receptors=receptors, remove_heterogens=False, batch_size=8),
            "minimize_receptors": lambda: chem.minimize_receptors(
                receptors=receptors, max_iterations=50, batch_size=8),
        }
        for name, call in ops.items():
            if wanted and name not in wanted:
                continue
            results[name] = run_op(rt, name, call, seen_errors=seen)
        with_3d = rt.molecules.count(rt.molecules.filter(ligands, filters={"has_3d": True}))
        print(f"ligands with has_3d after the ops: {with_3d}/{rt.molecules.count(ligands)}")
    finally:
        try:
            rt.shutdown()
        except Exception:
            pass
    failed = [name for name, ok in results.items() if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} ops passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
