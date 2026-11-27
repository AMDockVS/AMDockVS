"""Dock a sharded library on the vHPC SLURM lab (or its Grid Engine one, `--scheduler sge`) as one campaign: prep, VS, top-N, off-target.

Run with MSDD_312 and PYTHONPATH holding this checkout and the ms_flow, ms_env, ms_components
and ms_contactmap checkouts; the lab must be idle. The library is imported here, and one of
its shards prepared, with the app's APIs; the docking is `run_shards` on a `queue` executor. The chemistry the lab
lacks goes into the campaign's directory by a job of its queue, never into /opt/venv.

    python examples/vhpc_campaign.py ~/PycharmProjects/vhpc --flow ../ms_flow --env ../ms_env \
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
VINA_URL = "https://github.com/ccsb-scripps/AutoDock-Vina/releases/download/v1.2.7/vina_1.2.7_linux_x86_64"
# 10 shards of 10: small, so the lab run is cheap; 100 distinct molecules.
SMILES = [f"{'C' * (i + 1)}c1ccc({tail})cc1" for i in range(10)
          for tail in ("O", "N", "F", "Cl", "Br", "C", "CO", "CN", "C(=O)O", "OC")]


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
    local = lab / "workspace" / f"campaign-amdock-{uuid.uuid4().hex[:8]}"
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
    from ms_flow import campaign as flow_campaign
    from ms_flow.core.data import catalog

    from amdockvs.models import DockingResultRecord, ScreeningTarget

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
        f"/opt/venv/bin/python -m pip install -q --target {there}/extra pyarrow rdkit meeko ms_adtools scipy pandas gemmi\n"
        f"/opt/venv/bin/python -c \"import urllib.request as u; u.urlretrieve('{VINA_URL}', '{there}/vina')\"\n"
        f"chmod +x {there}/vina && {there}/vina --version\n"
        f"PYTHONPATH={paths} /opt/venv/bin/python -c 'import amdockvs.docking.shard_tasks'\n")
    print("extra:", _sbatch(host, f"{there}/extra.sh", scheduler=args.scheduler), flush=True)

    (local / "lib.smi").write_text("SMILES,Name\n" + "".join(f"{s},m{i}\n" for i, s in enumerate(SMILES)))
    rt = AMDockVSRuntime()
    rt.create_project(name="vhpc-campaign", folder=local / "project")
    try:
        here = "thread"
        rt.wait_for_jobs(rt.loader.shard_ligands([local / "lib.smi"], shard_size=10, executor_name=here), timeout_s=600)
        for step in ("standardize", "generate_3d"):
            _ok(rt.chemistry.run_shard_pipeline([step], executor_name=here, wait=True))
        db = rt.workspace.project_db
        assert catalog.count(db, catalog.shard_spec()) == 10, catalog.files(db, catalog.shard_spec())
        # Nothing prepared here: the campaign prepares every shard there, and it stays there.
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

        on, against = rt.molecules.create_set([target], name="target"), rt.molecules.create_set([off], name="off")

        sent = []  # every name a campaign sends (tar or rsync)
        upload = flow_campaign.Campaign._upload

        def counted(self, names, make=False):
            sent.extend(name for name in names if name not in ("job.sh", "manifest.sha"))
            return upload(self, names, make)

        flow_campaign.Campaign._upload = counted

        def remote_rows():
            return [row for role in catalog.roles(db) for row in catalog.files(db, catalog.shard_spec(role=role))
                    if row["location"]]

        def dock(run_id, cap=3):
            started = time.monotonic()
            job = rt.docking.run_shards(
                receptor_set=on, off_target_set=against,
                hit_threshold=0.0, hit_cap=cap, exhaustiveness=1, num_modes=1, vina_command=f"{there}/vina", vina_cpu=1,
                seed=1, run_id=run_id, executor_name="lab")
            row = rt.wait_for_job(job, poll_s=1)
            _ok(row)
            with ws.project_db.get_session() as session:
                hits = dict(session.exec(select(DockingResultRecord.receptor_molecule_id, func.count())
                                         .group_by(DockingResultRecord.receptor_molecule_id)).all())
                targets = [(t.receptor_molecule_id, t.state, t.ligands_done, t.imported)
                           for t in session.exec(select(ScreeningTarget).order_by(ScreeningTarget.id)).all()]
            print(run_id, job, f"{time.monotonic() - started:.0f} s", hits, targets, flush=True)
            return job, hits

        first, hits = dock("vhpc-a")
        assert hits == {target: 3, off: 3}, hits
        far_rows = remote_rows()
        print("sent:", len(sent), "prepared there:", [(r["id"], r["role"], r["location"]) for r in far_rows], flush=True)
        assert far_rows and {r["location"] for r in far_rows} == {"lab"}, far_rows
        prepared_role = far_rows[0]["role"]
        assert catalog.count(db, catalog.shard_spec(role=prepared_role)) == 0  # here, nothing to open
        jobs = "qstat -s z | wc -l" if sge else "sacct -n -X -P -o JobID | wc -l"
        jobs_before = host.run(jobs).decode().strip()
        again, hits_again = dock("vhpc-b")  # another run id, the same campaign: found, nothing queued, moved
        # Found again by its identity (prep included: what the host already prepared it skips there,
        # so this side's elements never change), and what it did lands in this run too.
        assert again == first and hits_again == {target: 6, off: 6}, (again, first, hits_again)
        assert host.run(jobs).decode().strip() == jobs_before
        sent.clear()
        # Another campaign (another selection, the same protocol): nothing to send, nothing to prepare.
        third, _ = dock("vhpc-c", cap=2)
        assert third != first
        assert remote_rows() == far_rows, remote_rows()
        print("second campaign sent:", sent, flush=True)
        assert not [name for name in sent if "shards" in name], sent

        # Adoption (idle): pull one file, checksum checked, and the local reader sees it.
        row = far_rows[0]
        sha = host.run(f"sha256sum {row['path']}").decode().split()[0]
        mine = local / "adopted" / Path(row["path"]).name
        mine.parent.mkdir()
        mine.write_bytes(host.run(f"cat {row['path']}"))
        assert catalog.adopt(db, row["id"], mine, checksum=sha)
        assert catalog.count(db, catalog.shard_spec(role=prepared_role)) == row["n_shards"]
        print(host.run(f"ls {there}/queue; " + ("qstat -s z | tail -n 3" if sge else
                       "sacct -n -X -P -o JobID,State,NNodes | tail -n 3")).decode())
    finally:
        rt.shutdown()
    print("ok", local)


if __name__ == "__main__":
    main()
