"""A shard import judges every split-off fragment by the import's filters, as a row import does.

Worth a test because the shard route prepared and appended the co-components without looking:
with Include SMARTS = N, naphthol landed in the screening library next to the anilide that
matched. And a record whose own molecule was filtered out dropped its fragments unseen.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rdkit")

from rdkit import Chem  # noqa: E402

PAIR = "c1ccccc1C(=O)Nc1ccccc1.c1ccc2ccccc2c1O"  # benzanilide + naphthol: both drug-sized


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


def _stored_names(runtime, path, prefilter):
    from ms_flow.core.data.shard import Shard
    from sqlmodel import select

    from amdockvs.models import ScreeningShard

    jobs = runtime.loader.shard_ligands([str(path)], executor_name="thread", prefilter=prefilter)
    states = runtime.wait_for_jobs(list(jobs), timeout_s=60)
    assert all(str(state.status).endswith("completed") for state in states.values()), states
    with runtime.workspace.project_db.get_session() as session:
        paths = [row.path for row in session.exec(select(ScreeningShard)).all()]
    names = []
    for shard_path in paths:
        with Shard.open(shard_path) as shard:
            for record in shard:
                raw = record.load()
                first = raw.split("\n", 1)[0] if "\n" in raw.strip() else raw.split(None, 1)[-1]
                names.append(first.strip())
    return sorted(names)


@pytest.mark.parametrize("suffix", [".smi", ".sdf"])
def test_a_split_fragment_must_pass_the_filter_itself(runtime, tmp_path, suffix):
    path = tmp_path / f"pair{suffix}"
    if suffix == ".smi":
        path.write_text(f"{PAIR} pair\n", encoding="utf-8")
    else:
        mol = Chem.MolFromSmiles(PAIR)
        mol.SetProp("_Name", "pair")
        with Chem.SDWriter(str(path)) as writer:
            writer.write(mol)
    assert _stored_names(runtime, path, {"split_fragments": True, "include_smarts": ["N"]}) == ["pair"]


def test_a_fragment_that_passes_is_kept_when_its_record_did_not(runtime, tmp_path):
    path = tmp_path / "pair.smi"
    path.write_text(f"{PAIR} pair\n", encoding="utf-8")
    assert _stored_names(runtime, path, {"split_fragments": True, "exclude_smarts": ["N"]}) == ["pair [frag02]"]


def test_without_a_filter_both_are_kept(runtime, tmp_path):
    path = tmp_path / "pair.smi"
    path.write_text(f"{PAIR} pair\n", encoding="utf-8")
    assert _stored_names(runtime, path, {"split_fragments": True}) == ["pair", "pair [frag02]"]
