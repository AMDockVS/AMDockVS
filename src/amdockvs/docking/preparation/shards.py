"""Ligand preparation over a shard: one container in, one PDBQT container out.

The `vs` twin is `service.prepare_entities_rows`, which writes one `.pdbqt` file per ligand and
one `ligand_preps` row per molecule. Neither scales to a screening library — that is the whole
reason the library is sharded. What travels instead is the shard: the prepared PDBQT text sits
at the *same id* as the molecule it came from, so nothing is renumbered and nothing is joined.

A molecule that fails preparation leaves a hole at its id, its reason in the file's dropped
sidecar (ms_flow `read_dropped`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ms_flow.core.data.shard import Serializer, Shard, ShardWriter
from amdockvs.core.constants import ShardKind

from amdockvs.chemistry.pipeline import run_pipeline
from amdockvs.chemistry.shards import read_shard
from amdockvs.docking.preparation.adtools import prepare_ligand_vina_pdbqt_from_mol

_SMILES_REMARK = "REMARK SMILES "
_SMILES_IDX_REMARK = "REMARK SMILES IDX"


def result_text(result) -> str:
    """The prepared payload as text. A step that only writes a file gets read, then unlinked.

    Meeko hands back the PDBQT string, so the second branch is dead today. It is here because
    the rule for every preparation step is the same: what it emits ends up *inside* the shard,
    never beside it as one file per ligand.
    """
    payload = str(getattr(result, "payload", "") or "")
    if payload.strip():
        return payload
    path = getattr(getattr(result, "artifact", None), "path", None)
    if path is None:
        raise RuntimeError("Ligand preparation returned neither payload nor file.")
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def _has_3d(mol) -> bool:
    if mol is None or isinstance(mol, Exception) or mol.GetNumConformers() == 0:
        return False
    return any(bool(mol.GetConformer(index).Is3D()) for index in range(mol.GetNumConformers()))


def _with_3d(mols: Sequence[Any]) -> list[Any]:
    """Embed the ones that have no conformer, leave the ones that do exactly as they are.

    A library imported as SMILES has no coordinates and Meeko refuses without them. Re-embedding
    the whole batch instead would throw away the conformer an earlier chemistry pass produced.
    """
    missing = [index for index, mol in enumerate(mols) if mol is not None and not _has_3d(mol)]
    if not missing:
        return list(mols)
    out = list(mols)
    embedded = run_pipeline([out[index] for index in missing], [("generate_3d", {})])
    for index, mol in zip(missing, embedded):
        out[index] = mol
    return out


def _reason(exc: BaseException) -> str:
    """One line, so grouping actually groups: Meeko reports the same fault per molecule."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


def prepare_ligand_shard(
    shard_in: str | Path,
    shard_out: str | Path,
    *,
    engine: str = "ad4",
    ensure_3d: bool = True,
    ids=None,
    scope: int | None = None,
    origin: int | None = None,
) -> dict[str, int]:
    """Every molecule of one shard as PDBQT text, in one derived file of one group: the same ids,
    ``origin`` = the source shard's id. A molecule that fails is dropped with its reason (the
    file's dropped sidecar). No db, no per-ligand file. `ids`: the shard's range inside
    `shard_in` (`shard_ids`), None = the whole file."""
    if str(engine).strip().lower() != "ad4":
        raise ValueError(f"Unsupported shard preparation engine: {engine}")
    source = Path(shard_in).expanduser().resolve()
    with Shard.open(source, ids=ids) as shard:
        base_id, slot_count = shard.base_id, shard.slot_count
    if ids is not None:
        base_id, slot_count = int(ids[0]), int(ids[1]) - int(ids[0])
    record_ids, mols = read_shard(source, ids)
    if ensure_3d:
        mols = _with_3d(mols)

    out_path = Path(shard_out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = failed = 0
    with ShardWriter(out_path, dataset_id=None, shard_id=0, base_id=base_id, kind=ShardKind.PDBQT,
                     serializer=Serializer.UTF8, slot_count=slot_count,
                     metadata={"prepared_engine": engine}) as writer:
        for id_, mol in zip(record_ids, mols):
            if mol is None or isinstance(mol, Exception):
                writer.drop(id_, _reason(mol) if isinstance(mol, Exception) else "unreadable record")
                failed += 1
                continue
            try:
                text = result_text(prepare_ligand_vina_pdbqt_from_mol(mol))
            except Exception as exc:  # noqa: BLE001 - one unpreparable ligand is a hole, not a lost shard
                writer.drop(id_, _reason(exc))
                failed += 1
                continue
            writer.add(id_, text.encode("utf-8"))
            written += 1
        writer.end_group(scope=scope, base_id=base_id, slot_count=slot_count, origin=origin)
    return {"n_input": len(record_ids), "n_records": written, "n_failed": failed}


def smiles_from_pdbqt(text: str) -> str:
    """The SMILES Meeko stamps into the PDBQT remarks.

    A hit has to become a molecule row, and a row without a SMILES cannot be drawn, searched or
    exported (§4). The prepared record already carries it, so nothing has to be recomputed or
    joined back to the source shard.
    """
    parts = [
        line[len(_SMILES_REMARK):].strip()
        for line in str(text).splitlines()
        if line.startswith(_SMILES_REMARK) and not line.startswith(_SMILES_IDX_REMARK)
    ]
    return "".join(parts)


def extract_ligands(
    shard_path: str | Path,
    dest_dir: str | Path,
    *,
    start: int = 0,
    count: int | None = None,
    record_ids: Sequence[int] | None = None,
    ids=None,
) -> list[dict[str, Any]]:
    """A slice of a prepared shard as `.pdbqt` files — the only shape a docking engine reads.

    The inverse of the preparation rule and its exception: what a stage *produces* goes into the
    shard, but what an external binary *consumes* has to be a file. It exists for the length of
    one chunk and the caller deletes it. `ids`: the shard's range in its file (`prepared_ids`),
    None = the whole file.

    ponytail: the slice is reached by walking record indices from 0. That is a header-side walk
    (no payload is loaded until the slice starts), so at 1000 records per shard it costs
    nothing; a per-record offset index would only pay off on far bigger shards.
    """
    dest = Path(dest_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    ligands: list[dict[str, Any]] = []
    selected_ids = None if record_ids is None else {int(value) for value in record_ids}
    with Shard.open(Path(shard_path).expanduser().resolve(), ids=ids) as shard:
        for offset, record in enumerate(shard):
            if offset < int(start):
                continue
            if selected_ids is not None and int(record.id) not in selected_ids:
                continue
            if count is not None and len(ligands) >= int(count):
                break
            text = record.load()
            if isinstance(text, (bytes, bytearray)):
                text = bytes(text).decode("utf-8")
            path = dest / f"{record.id}.pdbqt"
            path.write_text(text, encoding="utf-8")
            ligands.append({"id": int(record.id), "path": str(path), "smiles": smiles_from_pdbqt(text)})
            if selected_ids is not None and len(ligands) == len(selected_ids):
                break
    return ligands


__all__ = [
    "extract_ligands",
    "prepare_ligand_shard",
    "result_text",
    "smiles_from_pdbqt",
]
