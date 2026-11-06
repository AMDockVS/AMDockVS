"""Dock ligand rows on the vHPC SLURM lab (or its Grid Engine one, `--scheduler sge`): their chunks travel in transport shards, as one campaign.

Run with MSDD_312 and PYTHONPATH holding this checkout and the ms_flow, ms_env, ms_components
and ms_contactmap checkouts; the lab must be idle. The ligands are imported as rows and prepared
here, with the app's APIs; the docking is `docking.run` on a `queue` executor. The chemistry the lab
lacks goes into the campaign's directory by a job of its queue, never into /opt/venv.

    python examples/vhpc_rows_campaign.py ~/PycharmProjects/vhpc --flow ../ms_flow --env ../ms_env \
        --components ../ms_components --contactmap ../ms_contactmap [--scheduler sge]
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data" / "docking"
SMILES = ["CCCCCCO", "c1ccccc1CC", "c1ccc2ccccc2c1", "CC(=O)Nc1ccccc1", "CC(=O)Oc1ccccc1C(=O)O",
          "CN1C=NC2=C1C(=O)N(C)C(=O)N2C", "c1ccc(cc1)S(=O)(=O)N", "CCOC(=O)c1ccccc1"]


def _sbatch(host, script: str, seconds: float = 1200, scheduler: str = "slurm") -> str:
    if scheduler == "sge":  # qsub waits for the job and exits with its status
        return host.run(f"qsub -sync y -terse -cwd -S /bin/bash {script}", timeout=seconds).decode().split()[0]
    job = host.run(f"sbatch --parsable {script}").decode().strip().split(";")[0]
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        state = (host.run(f"sacct -n -X -j {job} -o State -P").decode().split() or ["PENDING"])[0]
        if state == "COMPLETED":
            return job
        if state in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}:
            raise RuntimeError(f"job {job} of {script}: {state}")
        time.sleep(3)
    raise TimeoutError(f"job {job} of {script}")


def _ok(row) -> None:
    assert row.status == "completed", (row.status, getattr(row, "error", None))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vhpc", type=Path)
    for name in ("flow", "env", "components", "contactmap"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--scheduler", choices=("slurm", "sge"), default="slurm")
    args = parser.parse_args()
    lab = args.vhpc.resolve() / "lab"
    local = lab / "workspace" / f"rows-amdock-{uuid.uuid4().hex[:8]}"
    there = f"/workspace/{local.name}"
    local.mkdir(parents=True)
    os.environ["MOLSUITE_HOME"] = str(local / "client" / "molsuite")  # not this user's projects
    os.environ["AMDOCK_HOME"] = str(local / "client" / "amdock")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("AMDOCK_DISABLE_PYMOL", "1")

    from ms_env import Host
    from ms_flow._api_impl.job_api import build_executor
    from sqlmodel import func, select

    from amdockvs import AMDockVSRuntime
    from amdockvs.models import DockingResultRecord
    from amdockvs.docking.campaign import transport_folder

    trees = {"amdock": REPO, "flow": args.flow, "env": args.env, "components": args.components,
             "contactmap": args.contactmap}
    for name, source in trees.items():
        shutil.copytree(source.resolve() / "src", local / name, ignore=shutil.ignore_patterns("__pycache__"))
    paths = ":".join(f"{there}/{name}" for name in (*trees, "extra"))
    sge = args.scheduler == "sge"
    host = Host("user@127.0.0.1", ssh=(
        "ssh", "-p", "2223" if sge else "2222", "-i", str(lab / "runtime/id_ed25519"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={lab / 'runtime/known_hosts'}"))
    head = (f"#!/bin/sh\n#$ -j y\n#$ -o {there}/extra.log\n" if sge else
            "#!/bin/sh\n#SBATCH --nodes=1\n#SBATCH --cpus-per-task=1\n#SBATCH --mem=2048\n#SBATCH --time=00:20:00\n"
            f"#SBATCH --output={there}/extra-%j.log\n")
    (local / "extra.sh").write_text(
        f"{head}set -eu\n"
        f"/opt/venv/bin/python -m pip install -q --target {there}/extra pyarrow rdkit meeko vina scipy pandas gemmi\n"
        f"PYTHONPATH={paths} /opt/venv/bin/python -c 'import amdockvs.docking.tasks'\n")
    print("extra:", _sbatch(host, f"{there}/extra.sh", scheduler=args.scheduler), flush=True)

    (local / "lib.smi").write_text("SMILES,Name\n" + "".join(f"{s},m{i}\n" for i, s in enumerate(SMILES)))
    rt = AMDockVSRuntime()
    rt.create_project(name="vhpc-campaign", folder=local / "project")
    try:
        here = "thread"
        rt.wait_for_jobs(rt.loader.load_ligands([local / "lib.smi"], batch_size=4, executor_name=here), timeout_s=600)
        _ok(rt.wait_for_job(rt.chemistry.generate_3d_ligands(batch_size=4, executor_name=here)))
        _ok(rt.wait_for_job(rt.docking.prepare_ligands(batch_size=4, executor_name=here)))
        loaded = rt.loader.load_receptors([DATA / "1iep_receptorH.pdb", DATA / "1iep_receptor.pdbqt"], executor_name=here)
        assert all(row.status == "completed" for row in rt.wait_for_jobs(loaded, timeout_s=600).values())
        receptors = rt.molecules.select(role="receptor", excluded=False)
        target, off = sorted(int(row.id) for row in rt.molecules.stream(receptors))
        _ok(rt.wait_for_job(rt.docking.prepare_receptors(receptor_set=receptors, executor_name=here)))
        values = {key.strip(): value for key, value in (line.split("=") for line in
                  (DATA / "1iep_receptor.box.txt").read_text().splitlines() if "=" in line)}
        center = tuple(float(values[f"center_{a}"]) for a in "xyz")
        size = tuple(float(values[f"size_{a}"]) for a in "xyz")
        for receptor in (target, off):
            rt.docking.set_grid(receptor_id=receptor, center=center, size=size)

        ws = rt.workspace
        ws.save_host("lab", address="user@127.0.0.1", workdir=f"{there}/queue", env="/opt/venv", keep_after=7)
        setup = (f"#$ -pe smp 2\n#$ -j y\n#$ -o {there}/campaign.log\nexport PYTHONPATH={paths}" if sge else
                 "#SBATCH --no-requeue\n#SBATCH --nodes=2\n#SBATCH --ntasks-per-node=1\n"
                 "#SBATCH --cpus-per-task=2\n#SBATCH --mem=2048\n#SBATCH --time=00:20:00\n"
                 f"#SBATCH --output={there}/campaign-%j.log\nexport PYTHONPATH={paths}")
        ws.save_executor("lab", kind="queue", hosts=["lab"], scheduler=args.scheduler, poll_seconds=2,
                         shared_disk=False, setup=setup)
        far = build_executor("lab", ws.settings_manager.settings)
        far.remote = host  # ponytail: the lab's ssh port and key; a real cluster is in ~/.ssh/config
        ws.engine.register("lab", far)

        ligands = len(SMILES)

        def dock(run_id="vhpc-rows", skip_existing=True):
            started = time.monotonic()
            job = rt.docking.run(receptor_set=receptors, batch_size=3, exhaustiveness=1, num_modes=2,
                                 vina_backend="python", vina_cpu=1, seed=1, run_id=run_id,
                                 skip_existing=skip_existing, executor_name="lab")
            _ok(rt.wait_for_job(job, poll_s=1))
            left = list(transport_folder(ws.project_db).glob("*"))
            assert not left, left  # an ended campaign takes its transport shards away
            with ws.project_db.get_session() as session:
                done = dict(session.exec(select(DockingResultRecord.receptor_molecule_id,
                                                func.count(func.distinct(DockingResultRecord.ligand_molecule_id)))
                                         .group_by(DockingResultRecord.receptor_molecule_id)).all())
                poses = session.exec(select(func.count()).select_from(DockingResultRecord)).one()
            print(job, f"{time.monotonic() - started:.0f} s", done, poses, flush=True)
            return job, done, poses

        first, done, poses = dock()
        assert done == {target: ligands, off: ligands}, done
        assert poses > 2 * ligands, poses  # every mode came back, not rank 1 only
        try:
            dock()
        except RuntimeError as error:  # every pair has a result: nothing is packed or queued
            assert "Nothing to dock" in str(error), error
        else:
            raise AssertionError("a docked scope was submitted again")
        # Forced, under another run id: docked again there, and its rows replace the older ones here.
        again, done_again, poses_again = dock("vhpc-rows-b", skip_existing=False)
        assert again != first and (done_again, poses_again) == (done, poses), (done_again, poses_again)
        print(host.run(f"ls {there}/queue; " + ("qacct -o user | tail -n 1" if sge else
                       "sacct -n -X -P -o JobID,State,NNodes | tail -n 3")).decode())
    finally:
        rt.shutdown()
    print("ok", local)


if __name__ == "__main__":
    main()
