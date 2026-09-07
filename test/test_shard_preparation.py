"""Preparing a sharded library: molecules in, PDBQT text out, ids kept, nothing beside the shard."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("meeko")
from ms_flow.core.data.shard import PayloadKind, Serializer, Shard, ShardWriter

from amdockvs.docking.preparation.shards import prepare_ligand_shard, result_text


def _smiles_shard(path: Path, smiles=("CCO", "c1ccccc1O")) -> Path:
    with ShardWriter(
        path,
        dataset_id=None,
        shard_id=0,
        base_id=0,
        kind=PayloadKind.SMILES,
        serializer=Serializer.UTF8,
        slot_count=len(smiles),
        metadata={"source": "lib.smi"},
    ) as writer:
        for logical_id, text in enumerate(smiles):
            writer.add(logical_id, text.encode())
    return path


def test_a_smiles_shard_comes_out_as_pdbqt_at_the_same_ids(tmp_path):
    out = tmp_path / "prepared" / "shard_0.mshard"
    stats = prepare_ligand_shard(_smiles_shard(tmp_path / "shard_0.mshard"), out)
    assert stats == {"n_input": 2, "n_records": 2, "n_failed": 0, "failures": {}}
    with Shard.open(out) as shard:
        assert shard.kind == PayloadKind.PDBQT
        records = {record.id: record.load() for record in shard}
    assert sorted(records) == [0, 1]
    assert all("ATOM" in str(text) for text in records.values())
    # 3D was generated in place: no per-ligand file was left anywhere.
    assert list(out.parent.iterdir()) == [out]


def test_re_preparing_reads_the_molecules_again_not_the_pdbqt(tmp_path):
    first = tmp_path / "prepared" / "shard_0.mshard"
    prepare_ligand_shard(_smiles_shard(tmp_path / "shard_0.mshard"), first)
    stats = prepare_ligand_shard(first, tmp_path / "again" / "shard_0.mshard")
    assert stats["n_records"] == 2


def test_a_step_that_only_writes_a_file_gets_read_and_the_file_deleted(tmp_path):
    from types import SimpleNamespace

    written = tmp_path / "ligand.pdbqt"
    written.write_text("ATOM x\n")
    result = SimpleNamespace(payload="", artifact=SimpleNamespace(path=written))
    assert result_text(result) == "ATOM x\n"
    assert not written.exists()
