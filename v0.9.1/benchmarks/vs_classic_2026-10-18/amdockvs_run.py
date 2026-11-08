"""The same campaign through the AMDockVS Python API: import -> protonate+3D -> meeko -> QuickVina."""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path(sys.argv[1])
os.environ["HOME"] = str(OUT / "home")  # never register in the real ~/.molsuite
os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["AMDOCK_QVINA"] = "/home/mario/.amdockvs/tools/docking/qvina/2.1/qvina2.1"

from amdockvs import AMDockVSRuntime  # noqa: E402

CENTER, SIZE = (15.190, 53.903, 16.917), (20.0, 20.0, 20.0)
phases = {}


def timed(name, fn):
    start = time.monotonic()
    result = fn()
    phases[name] = round(time.monotonic() - start, 1)
    print(f"{name}: {phases[name]} s", flush=True)
    return result


def done(rows):
    rows = rows.values() if isinstance(rows, dict) else [rows]
    for row in rows:
        assert row.status == "completed", (row.status, row.chunks_done, row.chunks_failed, row.chunks_total)


if __name__ == "__main__":
    start = time.monotonic()
    rt = AMDockVSRuntime()
    try:
        timed("startup", lambda: rt.create_project(name="bench500", folder=OUT / "project"))
        timed("import", lambda: done(rt.wait_for_jobs([
            *rt.loader.load_ligands([HERE / os.environ.get("LIGS", "ligs500.sdf")]),
            *rt.loader.load_receptors([HERE / "1iep_receptor.pdbqt"]),
        ], timeout_s=1800, poll_s=0.2)))
        ligands = rt.molecules.select(role="ligand", excluded=False)
        receptors = rt.molecules.select(role="receptor", excluded=False)
        receptor_id = int(next(iter(rt.molecules.stream(receptors))).id)
        timed("protonate+3d", lambda: done(rt.wait_for_job(
            rt.chemistry.run_ligand_pipeline(["protonate", "generate_3d"], ligands=ligands), poll_s=0.2)))
        timed("meeko", lambda: done(rt.wait_for_jobs(
            [rt.docking.prepare_ligands(program="qvina", ligands=ligands)], timeout_s=1800, poll_s=0.2)))
        rt.docking.set_grid(receptor_id=receptor_id, center=CENTER, size=SIZE)
        timed("docking", lambda: done(rt.wait_for_job(rt.docking.run(
            program="qvina", ligands=ligands, receptors=receptors, box_center=CENTER, box_size=SIZE,
            engine_config={"exhaustiveness": 8, "num_modes": 9, "vina_cpu": 1, "seed": 0}), poll_s=0.5)))
        stats = rt.docking.result_stats()
        best = {h.ligand_name: h.score for h in rt.docking.top_hits(limit=1000)}
        (OUT / "scores.json").write_text(json.dumps(best))
        phases["total"] = round(time.monotonic() - start, 1)
        print(json.dumps({"phases": phases, "results": stats.total_results, "ligands_scored": len(best)}))
    finally:
        rt.shutdown()
