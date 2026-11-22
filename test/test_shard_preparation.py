"""Preparing a sharded library: molecules in, PDBQT text out, ids kept, nothing beside the shard."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("ms_adtools")
from ms_flow.core.data.shard import Serializer, Shard, ShardWriter, file_groups, read_dropped
from amdockvs.core.constants import ShardKind

from amdockvs.docking.preparation.shards import prepare_ligand_shard, result_text


def _smiles_shard(path: Path, smiles=("CCO", "c1ccccc1O")) -> Path:
    with ShardWriter(
        path,
        dataset_id=None,
        shard_id=0,
        base_id=0,
        kind=ShardKind.SMILES,
        serializer=Serializer.UTF8,
        slot_count=len(smiles),
        metadata={"source": "lib.smi"},
    ) as writer:
        for logical_id, text in enumerate(smiles):
            writer.add(logical_id, text.encode())
    return path


def test_a_smiles_shard_comes_out_as_pdbqt_at_the_same_ids(tmp_path):
    out = tmp_path / "prepared" / "shard_0.parquet"
    stats = prepare_ligand_shard(_smiles_shard(tmp_path / "shard_0.parquet"), out, scope=3, origin=9)
    assert stats == {"n_input": 2, "n_records": 2, "n_failed": 0}
    with Shard.open(out) as shard:
        assert shard.kind == ShardKind.PDBQT
        records = {record.id: record.load() for record in shard}
    assert sorted(records) == [0, 1]
    assert all("ATOM" in str(text) for text in records.values())
    assert [(g.base_id, g.slot_count, g.scope, g.origin) for g in file_groups(out)] == [(0, 2, 3, 9)]
    # 3D was generated in place: no per-ligand file (and, nothing failed, no dropped sidecar).
    assert list(out.parent.iterdir()) == [out]


def test_a_molecule_that_fails_is_dropped_with_its_reason(tmp_path):
    out = tmp_path / "prepared" / "shard_0.parquet"
    stats = prepare_ligand_shard(_smiles_shard(tmp_path / "shard_0.parquet", ("CCO", "not a smiles")), out)
    assert (stats["n_records"], stats["n_failed"]) == (1, 1)
    assert file_groups(out)[0].n_dropped == 1
    [(dropped, reason)] = read_dropped(out)
    assert dropped == 1 and reason


def test_a_step_that_only_writes_a_file_gets_read_and_the_file_deleted(tmp_path):
    from types import SimpleNamespace

    written = tmp_path / "ligand.pdbqt"
    written.write_text("ATOM x\n")
    result = SimpleNamespace(payload="", artifact=SimpleNamespace(path=written))
    assert result_text(result) == "ATOM x\n"
    assert not written.exists()
