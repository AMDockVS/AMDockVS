"""A shard import applies the preparation a row import would: Hs, 3D, canonical tautomer, and
fragment split. Without any of them the shard keeps the raw record bytes."""
from __future__ import annotations

import io

import pytest

pytest.importorskip("rdkit")

from rdkit import Chem  # noqa: E402


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from amdockvs.runtime import AMDockVSRuntime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOLSUITE_HOME", str(tmp_path / "molsuite"))
    monkeypatch.setenv("AMDOCK_HOME", str(tmp_path / "amdock"))
    monkeypatch.setenv("AMDOCK_OFFLINE", "1")
    rt = AMDockVSRuntime()
    rt.create_project(name="shards", folder=tmp_path / "project")
    yield rt
    rt.shutdown()


def _sdf(path, *items):
    with Chem.SDWriter(str(path)) as writer:
        for smiles, name, tags in items:
            mol = Chem.MolFromSmiles(smiles)
            mol.SetProp("_Name", name)
            for key, value in tags.items():
                mol.SetProp(key, value)
            writer.write(mol)
    return path


def _shard(runtime, path, **prefilter):
    """[(format, raw record)] of everything the import stored."""
    from ms_flow.core.data import catalog
    from ms_flow.core.data.shard import Shard

    from amdockvs.io.shards import FORMAT_FOR_KIND, shard_number

    jobs = runtime.loader.shard_ligands([str(path)], executor_name="thread", prefilter=prefilter or None)
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    rows = [(row["path"], shard_number(row)) for row in catalog.shards(runtime.workspace.project_db, whole=True)]
    out = []
    for shard_path, number in rows:
        with Shard.open(shard_path, shard=number) as shard:
            out += [(FORMAT_FOR_KIND.get(shard.kind, ""), record.load()) for record in shard]
    return out


def _mols(records):
    return [next(Chem.ForwardSDMolSupplier(io.BytesIO(raw.encode()), removeHs=False)) for _fmt, raw in records]


def test_add_hs_stores_explicit_hydrogens_and_keeps_tags(runtime, tmp_path):
    source = _sdf(tmp_path / "one.sdf", ("CCO", "ethanol", {"IC50": "100"}))
    [mol] = _mols(_shard(runtime, source, add_hs=True))
    assert mol.GetNumAtoms() == 9
    assert mol.GetProp("_Name") == "ethanol" and mol.GetProp("IC50") == "100"


def test_gen_3d_from_smiles_stores_a_3d_sdf(runtime, tmp_path):
    source = tmp_path / "lib.smi"
    source.write_text("CCO ethanol\n")
    records = _shard(runtime, source, gen_3d=True)
    assert [fmt for fmt, _raw in records] == ["sdf"]
    [mol] = _mols(records)
    assert mol.GetConformer().Is3D() and mol.GetProp("_Name") == "ethanol"


def test_canonical_tautomer_on_smiles_stays_smiles(runtime, tmp_path):
    from rdkit.Chem.MolStandardize import rdMolStandardize

    source = tmp_path / "lib.smi"
    source.write_text("C=C(O)C enol\n")
    records = _shard(runtime, source, canonical_tautomer=True)
    expected = Chem.MolToSmiles(rdMolStandardize.TautomerEnumerator().Canonicalize(Chem.MolFromSmiles("C=C(O)C")))
    assert records == [("smiles", f"{expected} enol")]


def test_split_fragments_stores_each_real_component(runtime, tmp_path):
    # Two organic components of comparable size: the second one is kept as its own record.
    source = _sdf(tmp_path / "salts.sdf", ("c1ccccc1O.c1ccncc1C", "pair", {}))
    names = sorted(mol.GetProp("_Name") for mol in _mols(_shard(runtime, source, split_fragments=True)))
    assert names == ["pair", "pair [frag02]"]


def test_without_preparation_the_raw_record_is_kept(runtime, tmp_path):
    source = tmp_path / "lib.smi"
    source.write_text("OCC ethanol\n")
    assert _shard(runtime, source) == [("smiles", "OCC ethanol")]


def test_shards_sharing_a_file_prepare_each_into_its_own_shard(runtime, tmp_path):
    """Three logical shards in one Parquet file: each prepares only its own id range, and the
    per-task parts are appended to one container as they arrive, no merge pass."""
    from pathlib import Path

    from ms_flow.core.data import catalog, container
    from ms_flow.core.data.shard import Shard

    from amdockvs.molecules.storage import iter_prepared_shards

    source = tmp_path / "lib.smi"
    source.write_text("".join(f"{'C' * (i + 1)}O mol{i}\n" for i in range(10)))
    jobs = runtime.loader.shard_ligands([str(source)], executor_name="thread", shard_size=4)
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    rows = sorted(catalog.shards(runtime.workspace.project_db, whole=True), key=lambda row: row["base_id"])
    assert [row["n_records"] for row in rows] == [4, 4, 2]
    assert len({row["path"] for row in rows}) == 1

    job = runtime.docking.prepare_ligand_shards(executor_name="thread")
    state = runtime.wait_for_jobs([job], timeout_s=120)[job]
    assert str(state.status).endswith("completed"), state
    prepared = sorted(iter_prepared_shards(runtime.workspace.project_db, engine="ad4"), key=lambda row: row["base_id"])
    assert [row["id"] for row in prepared] == [row["id"] for row in rows]
    assert len({row["prepared_path"] for row in prepared}) == 1
    path = Path(prepared[0]["prepared_path"])
    assert all(row["part"] for row in container.read_index(path)[1])
    assert sorted(p.name for p in path.parent.iterdir() if p.suffix != ".lock") == [path.name, path.name + ".index"]
    seen = []
    for row, source_row in zip(prepared, rows):
        assert row["prepared_shard"] is not None
        with Shard.open(row["prepared_path"], shard=row["prepared_shard"]) as shard:
            ids = [record.id for record in shard]
        lo, hi = source_row["base_id"], source_row["base_id"] + source_row["slot_count"]
        assert ids and all(lo <= i < hi for i in ids)
        seen += ids
    assert seen == list(range(10))


def test_a_resumed_prep_makes_the_same_part_ids(runtime, tmp_path, monkeypatch):
    """Trap 17: the part ids come from a token fixed in the params at submit, so a resumed (or
    replayed) forced run skips what it already committed instead of appending it again."""
    from amdockvs.docking.preparation import jobs

    source = tmp_path / "lib.smi"
    source.write_text("".join(f"{'C' * (i + 1)}O mol{i}\n" for i in range(6)))
    states = runtime.wait_for_jobs(list(runtime.loader.shard_ligands([str(source)], executor_name="thread",
                                                                     shard_size=4)), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states

    submitted = {}
    monkeypatch.setattr(runtime, "submit_job", lambda job, *, params, **_kw: submitted.update(params) or "job")
    runtime.docking.prepare_ligand_shards(force=True)
    assert submitted.get("run")

    config = {"project_db": runtime.workspace.project_db}
    parts = [[s["part"] for chunk in jobs.PrepareLigandShardsJobSpec.build_chunks(submitted, config)
              for s in chunk["shards"]] for _ in range(2)]
    assert parts[0] == parts[1] and len(parts[0]) == 2
    assert all(part.startswith(f"{submitted['run']}:") for part in parts[0])


def test_the_prep_job_counts_the_shards_it_prepares(runtime, tmp_path):
    """Its total is a chunk per shard of the picked files left to prepare: the monitor shows how
    far it is, not '?'. Nothing to prepare is the one chunk that closes the job."""
    from catalog_seed import seed_library

    from amdockvs.docking.preparation.jobs import PrepareLigandShardsJobSpec

    db = runtime.workspace.project_db
    first, _ = seed_library(db, tmp_path, scope=1, shards=3, name="a")
    seed_library(db, tmp_path, scope=2, shards=2, name="b")
    config = {"project_db": db}

    def count(**params):
        return PrepareLigandShardsJobSpec.count_chunks({"engine": "ad4", **params}, config)

    assert (count(), count(file_ids=[first]), count(file_ids=[])) == (5, 3, 1)
    chunks = list(PrepareLigandShardsJobSpec.build_chunks({"engine": "ad4", "file_ids": [first]}, config))
    assert len(chunks) == count(file_ids=[first])
