"""Durable, columnar result fragments for sharded docking campaigns."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa
import pyarrow.parquet as pq
from sqlmodel import select

from ms_flow.core.data.shard import Shard

from amdockvs.screening.materialize import TopNGate
from amdockvs.models import ScreeningShard, ScreeningShardRun
from amdockvs.core.paths import get_default_project_root
from amdockvs.core.vocab import TargetState

RESULT_DATASET_FORMAT = "amdock-docking-results-parquet-v1"

RESULT_SCHEMA = pa.schema([
    pa.field("ligand_id", pa.int64(), nullable=False),
    pa.field("receptor_id", pa.int64(), nullable=False),
    pa.field("score", pa.float64()),
    pa.field("score_type", pa.string(), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("engine", pa.string(), nullable=False),
    pa.field("has_pose", pa.bool_(), nullable=False),
    pa.field("smiles", pa.string(), nullable=False),
    pa.field("metrics_json", pa.string(), nullable=False),
    pa.field("pose_text", pa.string(), nullable=False),
    pa.field("pose_suffix", pa.string(), nullable=False),
])

SUMMARY_COLUMNS = (
    "ligand_id", "receptor_id", "score", "score_type", "status", "engine", "has_pose",
)
FULL_COLUMNS = tuple(field.name for field in RESULT_SCHEMA)
SUPPORTED_FILTERS = {
    "engine", "receptor_id", "receptor_id__in", "score__gte", "score__lte", "status",
}


def _safe_component(value: object, *, fallback: str) -> str:
    text = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value or ""))
    return text[:64] or fallback


def result_fragment_path(output_dir: str | Path, payload: Mapping[str, Any], shard: Mapping[str, Any]) -> Path:
    run_id = _safe_component(payload.get("run_id"), fallback="run")
    protocol = _safe_component(
        dict(payload.get("protocol_metadata") or {}).get("hash"), fallback="default"
    )
    receptor_id = int(payload.get("receptor_id") or 0)
    shard_index = int(shard.get("shard_index") or 0)
    return (
        Path(output_dir)
        / "runs"
        / run_id
        / f"proto_{protocol}"
        / f"receptor_{receptor_id:05d}"
        / f"shard_{shard_index:05d}.results.parquet"
    )


def local_pose_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_file():
        return path
    root = get_default_project_root()
    candidate = None if root is None or path.is_absolute() else root / path
    return candidate if candidate is not None and candidate.is_file() else None


def result_row(row: Mapping[str, Any], *, smiles: str) -> dict[str, Any]:
    metrics = dict(row.get("metrics") or {})
    pose_path = next(
        (
            path
            for value in (
                metrics.get("selected_pose_path"),
                row.get("pose_path"),
                metrics.get("selected_pose_pdbqt_path"),
            )
            if (path := local_pose_path(value)) is not None
        ),
        None,
    )
    metrics["selected_pose_path"] = ""
    metrics["selected_pose_pdbqt_path"] = ""
    return {
        "ligand_id": int(row.get("ligand_molecule_id") or 0),
        "receptor_id": int(row.get("receptor_molecule_id") or 0),
        "score": None if row.get("score") is None else float(row["score"]),
        "score_type": str(row.get("score_type") or ""),
        "status": str(metrics.get("status") or "done"),
        "engine": str(row.get("engine") or ""),
        "has_pose": pose_path is not None,
        "smiles": str(smiles or ""),
        "metrics_json": json.dumps(metrics, ensure_ascii=True, default=str),
        "pose_text": "" if pose_path is None else pose_path.read_text(encoding="utf-8", errors="replace"),
        "pose_suffix": ".pose" if pose_path is None else pose_path.suffix.lower(),
    }


class ResultFragmentWriter:
    """Append rows immediately and publish the Parquet fragment atomically on success."""

    def __init__(self, path: str | Path, *, metadata: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = self.path.with_name(f"{self.path.name}.tmp")
        self.temporary.unlink(missing_ok=True)
        schema = RESULT_SCHEMA.with_metadata({
            b"amdock.metadata": json.dumps(
                {"record_format": RESULT_DATASET_FORMAT, **dict(metadata)},
                ensure_ascii=True,
                default=str,
            ).encode("utf-8")
        })
        self._writer = pq.ParquetWriter(self.temporary, schema, compression="zstd")
        self._schema = schema

    def add(self, row: Mapping[str, Any]) -> None:
        # One row group per completed ligand makes the loose pose disposable immediately.
        payload = dict(row)
        payload.setdefault("has_pose", bool(payload.get("pose_text")))
        self._writer.write_table(pa.Table.from_pylist([payload], schema=self._schema))

    def __enter__(self) -> "ResultFragmentWriter":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        self._writer.close()
        if exc_type is None:
            self.temporary.replace(self.path)
        else:
            self.temporary.unlink(missing_ok=True)


def _project_path(project_root: str | Path, value: str) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else Path(project_root) / path


def result_metadata(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    if resolved.suffix.lower() == ".parquet":
        raw = (pq.ParquetFile(resolved).schema_arrow.metadata or {}).get(b"amdock.metadata", b"{}")
        return dict(json.loads(raw.decode("utf-8")))
    with Shard.open(resolved) as shard:
        return dict(shard.metadata or {})


def completed_result_receipts(
    project_db,
    *,
    run_id: str,
    protocol_hash: str,
) -> list[ScreeningShardRun]:
    with project_db.get_session() as session:
        return list(session.exec(
            select(ScreeningShardRun)
            .where(
                ScreeningShardRun.run_id == str(run_id),
                ScreeningShardRun.protocol_hash == str(protocol_hash),
                ScreeningShardRun.state == TargetState.DONE,
                ScreeningShardRun.result_path != "",
            )
            .order_by(ScreeningShardRun.id)
        ).all())


def _arrow_filters(filters: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    result: list[tuple[str, str, Any]] = []
    for key, value in filters.items():
        if key == "receptor_id__in":
            result.append(("receptor_id", "in", [int(item) for item in value]))
        elif key == "score__lte":
            result.append(("score", "<=", float(value)))
        elif key == "score__gte":
            result.append(("score", ">=", float(value)))
        elif key in {"receptor_id", "engine", "status"}:
            result.append((key, "=", int(value) if key == "receptor_id" else str(value)))
    return result


def _matches(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for key, expected in filters.items():
        if key == "receptor_id__in" and int(row.get("receptor_id") or 0) not in {int(v) for v in expected}:
            return False
        if key == "score__lte" and (row.get("score") is None or float(row["score"]) > float(expected)):
            return False
        if key == "score__gte" and (row.get("score") is None or float(row["score"]) < float(expected)):
            return False
        actual = row.get(key)
        if key == "status" and actual in (None, ""):
            actual = "done"
        if key in {"receptor_id", "engine", "status"} and str(actual) != str(expected):
            return False
    return True


def _legacy_payloads(path: Path) -> list[dict[str, Any]]:
    with Shard.open(path) as shard:
        return [json.loads(gzip.decompress(record.load())) for record in shard]


def _summary_rows(path: Path, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".parquet":
        return pq.read_table(
            path,
            columns=list(SUMMARY_COLUMNS),
            filters=_arrow_filters(filters) or None,
        ).to_pylist()
    return [
        {**row, "has_pose": bool(_pose(row)[0])}
        for row in _legacy_payloads(path)
        if _matches(row, filters)
    ]


def _selected_payloads(path: Path, keys: set[tuple[int, int]]) -> dict[tuple[int, int], dict[str, Any]]:
    ligand_ids = sorted({key[0] for key in keys})
    if path.suffix.lower() == ".parquet":
        rows = pq.read_table(
            path,
            columns=list(FULL_COLUMNS),
            filters=[("ligand_id", "in", ligand_ids)],
        ).to_pylist()
    else:
        rows = _legacy_payloads(path)
    return {
        (int(row.get("ligand_id") or 0), int(row.get("receptor_id") or 0)): row
        for row in rows
        if (int(row.get("ligand_id") or 0), int(row.get("receptor_id") or 0)) in keys
    }


def _pose(payload: Mapping[str, Any]) -> tuple[str, str]:
    if "pose_text" in payload:
        suffix = str(payload.get("pose_suffix") or ".pose").lower()
        if not suffix.startswith(".") or not suffix[1:].isalnum():
            suffix = ".pose"
        return str(payload.get("pose_text") or ""), suffix
    return str(payload.get("sdf") or ""), ".sdf"


def select_hit_refs(
    project_db,
    *,
    project_root: str | Path,
    run_id: str,
    protocol_hash: str,
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank using only lightweight columns and return shard/record identities."""
    cap = int(top_n)
    if cap <= 0:
        raise ValueError("top_n must be greater than zero.")
    normalized_filters = {"status": "done", **dict(filters or {})}
    unsupported = set(normalized_filters) - SUPPORTED_FILTERS
    if unsupported:
        raise ValueError(f"Unsupported result filters: {', '.join(sorted(unsupported))}.")

    receipts = completed_result_receipts(
        project_db, run_id=run_id, protocol_hash=protocol_hash
    )
    with project_db.get_session() as session:
        shards = {
            int(row.id): row
            for row in session.exec(select(ScreeningShard).where(
                ScreeningShard.id.in_([int(receipt.shard_id) for receipt in receipts])
            )).all()
        } if receipts else {}

    gate = TopNGate(cap=cap, threshold=None)
    for receipt in receipts:
        path = _project_path(project_root, receipt.result_path)
        if not path.is_file() or int(receipt.shard_id) not in shards:
            continue
        candidates = []
        for row in _summary_rows(path, normalized_filters):
            if row.get("score") is None or not bool(row.get("has_pose")):
                continue
            candidates.append({
                **row,
                "_result_shard": str(path),
                "_result_record_id": int(row.get("ligand_id") or 0),
                "_shard_id": int(receipt.shard_id),
            })
        gate.take(candidates)

    return gate.drain()


def select_hits(
    project_db,
    *,
    project_root: str | Path,
    run_id: str,
    protocol_hash: str,
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank projected score columns, then load poses only for the selected rows."""
    selected = select_hit_refs(
        project_db,
        project_root=project_root,
        run_id=run_id,
        protocol_hash=protocol_hash,
        filters=filters,
        top_n=top_n,
    )
    receipts = completed_result_receipts(
        project_db, run_id=run_id, protocol_hash=protocol_hash
    )
    with project_db.get_session() as session:
        shards = {
            int(row.id): row
            for row in session.exec(select(ScreeningShard).where(
                ScreeningShard.id.in_([int(receipt.shard_id) for receipt in receipts])
            )).all()
        } if receipts else {}
    receipt_by_path = {
        _project_path(project_root, receipt.result_path): receipt
        for receipt in receipts
    }
    metadata_by_path = {
        path: result_metadata(path)
        for path in {Path(row["_result_shard"]) for row in selected}
    }

    keys_by_path: dict[Path, set[tuple[int, int]]] = {}
    for row in selected:
        keys_by_path.setdefault(Path(row["_result_shard"]), set()).add(
            (int(row["ligand_id"]), int(row["receptor_id"]))
        )
    payloads = {
        path: _selected_payloads(path, keys)
        for path, keys in keys_by_path.items()
    }

    hits: list[dict[str, Any]] = []
    for row in selected:
        path = Path(row["_result_shard"])
        key = (int(row["ligand_id"]), int(row["receptor_id"]))
        payload = payloads.get(path, {}).get(key)
        receipt = receipt_by_path.get(path)
        source_shard = None if receipt is None else shards.get(int(receipt.shard_id))
        if payload is None or receipt is None or source_shard is None:
            continue
        pose_text, pose_suffix = _pose(payload)
        protocol = dict(metadata_by_path.get(path, {}).get("protocol") or {})
        protocol.setdefault("hash", str(protocol_hash))
        metrics_raw = payload.get("metrics_json", payload.get("metrics") or {})
        metrics = json.loads(metrics_raw) if isinstance(metrics_raw, str) else dict(metrics_raw)
        metrics.update({
            "run_id": str(run_id),
            "protocol": protocol,
            "shard_source": str(source_shard.source),
            "shard_index": int(source_shard.shard_index),
            "shard_record_id": int(payload.get("ligand_id") or 0),
        })
        ligand_id = int(payload.get("ligand_id") or 0)
        hits.append({
            "name": f"{Path(source_shard.source).stem or 'library'}:{ligand_id}",
            "source": str(source_shard.source),
            "source_index": ligand_id,
            "smiles": str(payload.get("smiles") or ""),
            "engine": str(payload.get("engine") or receipt.engine),
            "score": float(payload["score"]),
            "score_type": str(payload.get("score_type") or ""),
            "pose_rank": 1,
            "receptor_molecule_id": int(payload.get("receptor_id") or receipt.receptor_molecule_id),
            "metrics": metrics,
            "_pose_text": pose_text,
            "_pose_suffix": pose_suffix,
            "_result_shard": str(path),
            "_result_record_id": ligand_id,
            "_shard_id": int(receipt.shard_id),
        })
    return hits


__all__ = [
    "RESULT_DATASET_FORMAT",
    "ResultFragmentWriter",
    "completed_result_receipts",
    "local_pose_path",
    "result_fragment_path",
    "result_metadata",
    "result_row",
    "select_hit_refs",
    "select_hits",
]
