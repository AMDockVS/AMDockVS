"""The chemistry pipeline over a shard: one container in, one container out.

The `vs` twin is `service.transform_ligand_rows`, which loads by row id, writes per-molecule
artifacts and emits graph updates. None of that exists here: a shard has no `extra_data` and
no rows to update. What both share is `pipeline.run_pipeline`, and that is the point — the
cluster script runs the same function as the local job, so "it worked in local" means
something.

What a shard *does* have is ids. A molecule that fails a step leaves a hole at its id instead
of shifting every molecule after it one place up, so record 7 of the output is still the
seventh molecule of the source file, three stages later.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Sequence

from ms_flow.core.data.shard import Serializer, Shard, ShardWriter
from amdockvs.core.constants import ShardKind

from amdockvs.chemistry.pipeline import normalize_steps, run_pipeline
from amdockvs.io.shards import FORMAT_FOR_KIND, mol_from_record


def read_shard(shard_path: str | Path, shard=None, record_ids=None) -> tuple[list[int], list[Any]]:
    """`(ids, molecules)`, positionally aligned. Unreadable records come back as `None`,
    which `run_pipeline` carries through untouched — the id is what carries identity.
    `shard`: the shard's number inside the file (`amdockvs.shards.shard_number`), None = all;
    `record_ids`: only those (what the exclusions left), None = all."""
    ids: list[int] = []
    mols: list[Any] = []
    with Shard.open(Path(shard_path).expanduser().resolve(), shard=shard, record_ids=record_ids) as opened:
        input_format = FORMAT_FOR_KIND.get(opened.kind, "")
        for record in opened:
            ids.append(record.id)
            mols.append(mol_from_record(record.load(), input_format))
    return ids, mols


def _molblock(mol) -> str:
    from rdkit import Chem

    buffer = io.StringIO()
    writer = Chem.SDWriter(buffer)
    try:
        writer.write(mol)
    finally:
        writer.close()
    return buffer.getvalue()


def write_shard(
    shard_path: str | Path,
    ids: Sequence[int],
    mols: Sequence[Any],
    *,
    base_id: int,
    slot_count: int,
    shard_id: int = 0,
    metadata: dict[str, Any] | None = None,
    scope: int | None = None,
) -> int:
    """Write the molecules that survived, each at its own id. Returns how many were written.

    Failures are dropped here, not earlier: `run_pipeline` keeps them in place so the caller
    can count them, and this is the caller. Dropped means *absent*, not renumbered. ``scope``:
    the source's (ids restart per source, so a shard is named by scope and id).
    """
    path = Path(shard_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with ShardWriter(
        path,
        dataset_id=None,
        shard_id=shard_id,
        base_id=base_id,
        kind=ShardKind.SDF,  # whatever came in, what a pipeline emits is a molecule
        serializer=Serializer.UTF8,
        slot_count=slot_count,
        metadata=dict(metadata or {}),
        scope=scope,
    ) as writer:
        for id_, mol in zip(ids, mols):
            if mol is None or isinstance(mol, Exception):
                continue
            writer.add(id_, _molblock(mol).encode("utf-8"))
            written += 1
    return written


def transform_ligand_shard(shard_in: str | Path, shard_out: str | Path, steps, shard=None, *,
                           scope: int | None = None) -> dict[str, int]:
    """Run the step list over a whole shard. One file/CPU, no database in sight."""
    with Shard.open(Path(shard_in).expanduser().resolve(), shard=shard) as opened:
        header = {
            "base_id": opened.base_id,
            "slot_count": opened.slot_count,
            "shard_id": opened.shard_id,
            "metadata": dict(opened.metadata),
        }
        ids = [record.id for record in opened]
        mols = [mol_from_record(record.load(), FORMAT_FOR_KIND.get(opened.kind, "")) for record in opened]
    results = run_pipeline(mols, normalize_steps(steps))
    written = write_shard(
        shard_out,
        ids,
        results,
        base_id=header["base_id"],
        slot_count=header["slot_count"],
        shard_id=header["shard_id"],
        metadata=header["metadata"],
        scope=scope,
    )
    return {"n_input": len(mols), "n_records": written, "n_failed": len(mols) - written}


def promotion_records(payload: dict, progress_cb=None) -> dict:
    """The worker side of a promotion: the records named, out of one shard, as `promote_records`
    takes them (the structure already compressed). A record that no longer parses is left out."""
    from amdockvs.chemistry.descriptors import canonical_smiles
    from amdockvs.core.payloads import structure_payload

    wanted = [int(i) for i in payload.get("record_ids") or ()]
    if not wanted:
        return {"records": []}
    cluster_of = dict(zip(wanted, payload.get("cluster_ids") or ()))
    batch, library = payload.get("import_batch_id"), payload.get("library") or "library"
    ids, mols = read_shard(payload["shard_path"], payload.get("shard"), wanted)
    return {"records": [
        {"name": f"{library}:{record_id}", "import_batch_id": batch, "source_index": record_id,
         "smiles": canonical_smiles(mol), "structure": structure_payload(_molblock(mol), "sdf").packed(),
         "cluster_id": cluster_of.get(record_id)}
        for record_id, mol in zip(ids, mols) if mol is not None
    ]}


__all__ = ["promotion_records", "read_shard", "transform_ligand_shard", "write_shard"]
