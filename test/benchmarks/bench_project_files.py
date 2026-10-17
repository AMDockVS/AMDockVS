"""How many files a docked project has, and how long its import takes (plan 08, A1-4 + A8-4).

    python test/benchmarks/bench_project_files.py LIBRARY [--n 700000] [--workdir DIR]

LIBRARY is an SDF, imported whole with its tags (e.g. data/Enamine_H100K/Enamine_H100K.sdf),
or a SMILES/CSV file whose first N lines are imported. Through the app (process pool), then stores one pose per
ligand through the docking job's own sink: the payload bytes are the ligand's structure nine
times (nine modes), not a Vina run, which at 700k would take days and leaves no more files.
Counts the project's files, prints a JSON line and deletes the project.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
RECEPTOR = REPO / "data" / "docking" / "1iep_receptorH.pdb"
BATCH = 10_000  # 400 ligands a chunk x the job's flush of 25 chunks


def count_files(root: Path) -> tuple[int, int, dict[str, int]]:
    files, size, where = 0, 0, Counter()
    for folder, _dirs, names in os.walk(root):
        rel = Path(folder).relative_to(root)
        for name in names:
            files += 1
            size += os.path.getsize(Path(folder) / name)
            where[str(Path(*rel.parts[:2])) if rel.parts else "."] += 1
    return files, size, dict(where.most_common())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--n", type=int, default=700_000)
    ap.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()))
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="bench_files_", dir=args.workdir))
    os.environ["HOME"] = str(work / "home")  # the project registers here, not in the real ~/.molsuite
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")
    from ms_flow.sinks import table_sink
    from sqlmodel import func, select

    from amdockvs import AMDockVSRuntime
    from amdockvs.core.constants import SERIES_POSES
    from amdockvs.core.payloads import payload_store, structure_payload
    from amdockvs.models import DockingResultRecord, MoleculeRecord

    library = args.library.resolve()
    if library.suffix.lower() != ".sdf":
        library = work / "library.smi"
        with open(args.library, encoding="utf-8") as source, open(library, "w", encoding="utf-8") as out:
            for index, line in enumerate(source):
                if index == args.n:
                    break
                out.write(f"{line.split(',')[0].split()[0]} m{index}\n")

    report = {"library": args.library.name}
    rt = AMDockVSRuntime()
    try:
        project = work / "project"
        rt.create_project(name="bench_files", folder=project)
        start = time.monotonic()
        rt.wait_for_jobs(rt.loader.load_ligands([library]), timeout_s=6 * 3600)
        report["import_s"] = round(time.monotonic() - start, 1)
        rt.wait_for_jobs(rt.loader.load_receptors([RECEPTOR]), timeout_s=600)

        store = rt.workspace.project_db
        with store.get_session() as session:
            receptor_id = session.exec(select(MoleculeRecord.id).where(MoleculeRecord.is_receptor)).first()
            ligands = dict(session.exec(select(MoleculeRecord.current_payload_id, MoleculeRecord.id)
                                        .where(MoleculeRecord.is_ligand)).all())
            props = session.exec(select(func.count(func.distinct(MoleculeRecord.properties_payload_id)))).one()
        report.update(ligands=len(ligands), properties_payloads=props)

        start, rows = time.monotonic(), []
        sink = table_sink(model=DockingResultRecord, write_mode="bulk")
        for payload_id, data in payload_store(rt).iter_bytes(ligands):
            rows.append(DockingResultRecord.build_row(
                receptor_molecule_id=receptor_id, ligand_molecule_id=ligands[payload_id], engine="vina",
                score=-7.0, pose_payload_id=structure_payload(data * 9, "sdf", SERIES_POSES)))
            if len(rows) == BATCH:
                store.persist_output_spec(sink, rows)
                rows = []
        if rows:
            store.persist_output_spec(sink, rows)
        report["poses_s"] = round(time.monotonic() - start, 1)
        report["results"] = rt.docking.result_stats().total_results
        rt.shutdown()
        files, size, where = count_files(project)
        report.update(files=files, size_mb=round(size / 2**20), by_folder=where)
        print(json.dumps(report), flush=True)
        if os.environ.get("BENCH_LIST"):
            print(*sorted(str(p.relative_to(project)) for p in project.rglob("*") if p.is_file()), sep="\n")
    finally:
        rt.shutdown()
        shutil.rmtree(work)  # the data of a bench never stays
    assert report["results"] == report["ligands"], report
    return 0 if files < 100 else 1


if __name__ == "__main__":
    sys.exit(main())
