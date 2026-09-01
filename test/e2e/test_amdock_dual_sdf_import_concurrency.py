import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, "//src")

from amdockvs import AMDockVSRuntime


def _patch_fake_home(monkeypatch, fake_home: Path):
    monkeypatch.setenv("HOME", str(fake_home))


def _make_sdf(path: Path, count: int, prefix: str):
    from rdkit import Chem

    writer = Chem.SDWriter(str(path))
    try:
        for idx in range(count):
            smiles = "CCO" if idx % 2 == 0 else "CCN"
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            mol.SetProp("_Name", f"{prefix}_{idx:05d}")
            writer.write(mol)
    finally:
        writer.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_amdock_dual_sdf_import_concurrency_is_stable_and_ordered(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    _patch_fake_home(monkeypatch, tmp_path)

    sdf_a = tmp_path / "set_a.sdf"
    sdf_b = tmp_path / "set_b.sdf"
    _make_sdf(sdf_a, 220, "A")
    _make_sdf(sdf_b, 180, "B")

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="dual_sdf", folder=tmp_path / "project", description="dual sdf e2e")
        job_ids = runtime.loader.load_ligands([sdf_a, sdf_b], batch_size=40, executor_name="thread")
        # Both files stream through a single job now (N files != N jobs).
        assert len(job_ids) == 1

        final = runtime.wait_for_jobs(job_ids, timeout_s=240, poll_s=0.05)
        assert all(row.status == "completed" for row in final.values())

        ligands = list(runtime.molecules.stream(runtime.molecules.select(role="ligand")))
        assert len(ligands) == 400

        names = [row.name for row in ligands]
        assert sum(1 for name in names if name.startswith("A_")) == 220
        assert sum(1 for name in names if name.startswith("B_")) == 180
    finally:
        runtime.shutdown()
