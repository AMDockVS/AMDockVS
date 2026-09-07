"""Ligand preparation over a shard: one container in, one PDBQT container out.

The `vs` twin is `service.prepare_entities_rows`, which writes one `.pdbqt` file per ligand and
one `EngineState` row per molecule. Neither scales to a screening library — that is the whole
reason the library is sharded. What travels instead is the shard: the prepared PDBQT text sits
at the *same id* as the molecule it came from, so nothing is renumbered and nothing is joined.

A molecule that fails preparation leaves a hole at its id, exactly as in the chemistry pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from ms_flow.core.data.shard import PayloadKind, Serializer, Shard, ShardWriter

from amdockvs.chemistry.pipeline import run_pipeline
from amdockvs.chemistry.shards import read_shard
from amdockvs.docking.preparation.meeko import prepare_ligand_vina_pdbqt_from_mol

PREPARED_DIR = "prepared_{engine}"

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


def _source_shard(path: Path) -> Path:
    """Re-preparing reads the molecules, not the PDBQT: follow `prepared_from` back one step."""
    with Shard.open(path) as shard:
        if shard.kind != PayloadKind.PDBQT:
            return path
        origin = str(shard.metadata.get("prepared_from") or "")
    if not origin or not Path(origin).is_file():
        raise RuntimeError(f"{path}: prepared shard with no readable source to re-prepare from.")
    return Path(origin)


# One line per failed ligand, beside the shard it belongs to. A file per shard is a dozen files
# for a library that has millions of molecules, and it is greppable without opening a container.
FAILURE_LOG_SUFFIX = ".failures.jsonl"


def failure_log_path(shard_path: str | Path) -> Path:
    return Path(shard_path).with_suffix(Path(shard_path).suffix + FAILURE_LOG_SUFFIX)


def failure_report(failures: Sequence[tuple[int, str]]) -> dict[str, Any]:
    """How many failed and why, grouped — the summary that lives in the shard's header.

    The ids are not here: they are in the log beside the shard, which has no size limit. The
    header answers "is something wrong with this shard", the log answers "which ones".
    """
    reasons: dict[str, int] = {}
    for _id, reason in failures:
        reasons[str(reason)] = reasons.get(str(reason), 0) + 1
    return {"n_failed": len(failures), "reasons": reasons}


def write_failure_log(shard_path: str | Path, failures: Sequence[tuple[int, str]]) -> Path | None:
    """Every failed id with its reason, as JSONL beside the prepared shard. None if nothing failed."""
    if not failures:
        return None
    path = failure_log_path(shard_path)
    with path.open("w", encoding="utf-8") as handle:
        for id_, reason in failures:
            handle.write(json.dumps({"id": int(id_), "reason": str(reason)}) + "\n")
    return path


def read_failure_log(shard_path: str | Path) -> list[dict[str, Any]]:
    """The log back as rows, for whoever wants to look at the ligands that failed."""
    path = failure_log_path(shard_path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
) -> dict[str, int]:
    """Every molecule of one shard as PDBQT text, in one PDBQT shard. No db, no per-ligand file."""
    if str(engine).strip().lower() != "ad4":
        raise ValueError(f"Unsupported shard preparation engine: {engine}")
    source = _source_shard(Path(shard_in).expanduser().resolve())
    with Shard.open(source) as shard:
        header = {
            "base_id": shard.base_id,
            "slot_count": shard.slot_count,
            "shard_id": shard.shard_id,
            "metadata": dict(shard.metadata),
        }
    ids, mols = read_shard(source)
    if ensure_3d:
        mols = _with_3d(mols)

    out_path = Path(shard_out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    failures: list[tuple[int, str]] = []
    # Two passes over the same writer would mean two files; the report is built while writing
    # and stamped at close, which the container allows because the header is written last.
    with ShardWriter(
        out_path,
        dataset_id=None,
        shard_id=header["shard_id"],
        base_id=header["base_id"],
        kind=PayloadKind.PDBQT,
        serializer=Serializer.UTF8,
        slot_count=header["slot_count"],
        metadata={**header["metadata"], "prepared_from": str(source), "prepared_engine": engine},
    ) as writer:
        for id_, mol in zip(ids, mols):
            if mol is None or isinstance(mol, Exception):
                failures.append((id_, _reason(mol) if isinstance(mol, Exception) else "unreadable record"))
                continue
            try:
                text = result_text(prepare_ligand_vina_pdbqt_from_mol(mol))
            except Exception as exc:  # noqa: BLE001 - one unpreparable ligand is a hole, not a lost shard
                failures.append((id_, _reason(exc)))
                continue
            writer.add(id_, text.encode("utf-8"))
            written += 1
        if failures:
            writer.metadata["prep_failures"] = {
                **failure_report(failures),
                "log": failure_log_path(out_path).name,
            }
    write_failure_log(out_path, failures)
    return {
        "n_input": len(ids),
        "n_records": written,
        "n_failed": len(failures),
        "failures": failure_report(failures) if failures else {},
    }


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
) -> list[dict[str, Any]]:
    """A slice of a prepared shard as `.pdbqt` files — the only shape a docking engine reads.

    The inverse of the preparation rule and its exception: what a stage *produces* goes into the
    shard, but what an external binary *consumes* has to be a file. It exists for the length of
    one chunk and the caller deletes it.

    ponytail: the slice is reached by walking record indices from 0. That is a header-side walk
    (no payload is loaded until the slice starts), so at 1000 records per shard it costs
    nothing; a per-record offset index would only pay off on far bigger shards.
    """
    dest = Path(dest_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    ligands: list[dict[str, Any]] = []
    selected_ids = None if record_ids is None else {int(value) for value in record_ids}
    with Shard.open(Path(shard_path).expanduser().resolve()) as shard:
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
    "FAILURE_LOG_SUFFIX",
    "PREPARED_DIR",
    "extract_ligands",
    "failure_log_path",
    "failure_report",
    "read_failure_log",
    "write_failure_log",
    "prepare_ligand_shard",
    "result_text",
    "smiles_from_pdbqt",
]
