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
    from ms_flow.core.data.shard import Shard
    from sqlmodel import select

    from amdockvs.io.shards import FORMAT_FOR_KIND
    from amdockvs.models import ScreeningShard

    jobs = runtime.loader.shard_ligands([str(path)], executor_name="thread", prefilter=prefilter or None)
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    with runtime.workspace.project_db.get_session() as session:
        paths = [row.path for row in session.exec(select(ScreeningShard)).all()]
    out = []
    for shard_path in paths:
        with Shard.open(shard_path) as shard:
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
