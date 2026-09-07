"""§3 end to end: an `htpvs` import shards the library and never materializes a molecule.

The plan's check, run for real: 3 SDF files in, shards on disk, `screening_shards` populated,
`molecules` empty. Then the same step list that `run_ligand_pipeline` would run over rows,
run over the shards instead — and a second run that does nothing, because the shards are done.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ms_flow.query import db_pages

from amdockvs import AMDockVSRuntime
from amdockvs.molecules.storage import ShardStore, shard_scope_spec
from amdockvs.core.vocab import ProjectMode, ShardState


def _make_sdf(path: Path, count: int, prefix: str):
    from rdkit import Chem

    writer = Chem.SDWriter(str(path))
    try:
        for idx in range(count):
            mol = Chem.MolFromSmiles("CCO" if idx % 2 == 0 else "CCN")
            mol.SetProp("_Name", f"{prefix}_{idx:03d}")
            writer.write(mol)
    finally:
        writer.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_htpvs_import_shards_and_the_pipeline_runs_over_them(tmp_path, monkeypatch):
    pytest.importorskip("rdkit")
    monkeypatch.setenv("HOME", str(tmp_path))

    files = [tmp_path / f"lib_{name}.sdf" for name in "abc"]
    for index, path in enumerate(files):
        _make_sdf(path, 20, f"L{index}")

    runtime = AMDockVSRuntime()
    try:
        runtime.create_project(name="htp", folder=tmp_path / "project")
        store = ShardStore(runtime.molsuite.project_db)
        assert runtime.mode == ProjectMode.VS  # nothing imported yet
        runtime.amdock_configuration.set_global_value("shards.records_per_shard", 9)
        runtime.amdock_configuration.set_value("shards.records_per_shard", 8)
        assert runtime.amdock_configuration.get_source("shards.records_per_shard") == "project"

        job_ids = runtime.loader.shard_ligands(files, executor_name="thread")
        final = runtime.wait_for_jobs(job_ids, timeout_s=240, poll_s=0.05)
        assert all(row.status == "completed" for row in final.values())

        shards = list(store.iter_rows())
        assert len(shards) == 9  # 3 files x 20 molecules, 8 per shard -> 3 shards each
        assert store.record_count() == 60
        assert all(Path(row["path"]).is_file() for row in shards)
        assert {row["state"] for row in shards} == {ShardState.PENDING}

        # The whole point of the mode: no ligand ever became a row.
        assert runtime.molecules.count(runtime.molecules.select(role="ligand")) == 0
        # ...and that, not a flag chosen at creation, is what makes this a campaign project.
        assert runtime.mode == ProjectMode.HTPVS

        status = runtime.chemistry.run_shard_pipeline(
            ["standardize", "generate_3d", "minimize"], executor_name="thread", wait=True
        )
        assert status.status == "completed"

        done = list(db_pages(runtime.molsuite.project_db, shard_scope_spec(state=None)))
        assert len(done) == 9  # processed in place: 9 shards, not 18
        assert {row["state"] for row in done} == {ShardState.READY}
        assert store.record_count(shard_scope_spec(state=ShardState.READY)) == 60
        for row in done:
            output = Path(row["path"])
            assert output.is_file() and output.parent.name == "standardize+generate_3d+minimize"
        # 3D is what the pipeline was asked for, so the shard now carries conformers...
        from amdockvs.chemistry.shards import read_shard

        out_ids, out_mols = read_shard(done[0]["path"])
        assert out_mols and all(mol.GetNumConformers() > 0 for mol in out_mols)
        # ...and the ids came through the pipeline: a shard is addressed, not just stacked.
        source_shard = next(row for row in shards if row["shard_index"] == done[0]["shard_index"])
        in_ids, _in_mols = read_shard(source_shard["path"])
        assert out_ids and set(out_ids) <= set(in_ids)

        # Idempotency: nothing is pending, so a second run has no work to do.
        assert not list(store.iter_rows())
        again = runtime.chemistry.run_shard_pipeline(
            ["standardize"], executor_name="thread", wait=True
        )
        assert again.status == "completed"
        assert store.record_count(shard_scope_spec(state=None)) == 60
    finally:
        runtime.shutdown()
