"""Durable, columnar results of sharded docking campaigns.

What is docked lives in ms_flow, not in a table here: a done-set of shard ids per key
(`ms_flow.core.data.states`, kind `DONE_KIND`) and the result rows as table files of the catalog
(role `result_role(key)`), which the parent compacts to one file per key. A key is one target:
what a shard is docked against, and under which selection.

The worker that docks a shard writes the files, so what reads the project database (the ranking,
the hits) imports SQL where it runs, not here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from amdockvs.core.paths import get_default_project_root
from amdockvs.docking.results.metrics import SCORE_METRICS

RESULT_DATASET_FORMAT = "amdock-docking-results-parquet-v1"
DONE_KIND = "docking"


def state_key(*, engine: str, protocol_hash: str, receptor_id: Any, binding_site_id: Any = 0,
              input_key: str = "", selection: str = "") -> str:
    """The key of one target: engine, protocol, receptor, its site, what it was prepared as
    (`docking_input_key`: a receptor prepared again, or given a new box, is docked again) and the
    selection — empty for the library, `off_target_selection` for a stage docking a Top-N."""
    text = json.dumps([str(engine).strip().lower(), str(protocol_hash or ""), int(receptor_id or 0),
                       int(binding_site_id or 0), str(input_key or ""), str(selection or "")])
    return "dock-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def off_target_selection(reference_receptor_id: Any, run_id: str) -> str:
    """An off-target stage docks one reference run's Top-N, not the library: a key of its own,
    scoped to that run, so it neither satisfies nor replaces a library docking of its receptor."""
    return f"top:{int(reference_receptor_id or 0)}:{run_id}" if reference_receptor_id else ""


def result_role(key: str) -> str:
    """The catalog role of a key's result rows."""
    return f"table:{key}"


RESULT_SCHEMA = pa.schema([
    pa.field("ligand_id", pa.int64(), nullable=False),
    # Ligand ids restart in every import: with the shard id a row's ligand is unique.
    pa.field("shard_id", pa.int64(), nullable=False),
    pa.field("docking_run_id", pa.int64(), nullable=False),
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
    "ligand_id", "shard_id", "docking_run_id", "receptor_id", "score", "score_type", "status",
    "engine", "has_pose",
)
FULL_COLUMNS = tuple(field.name for field in RESULT_SCHEMA)
SUPPORTED_FILTERS = {
    "docking_run_id__in", "engine", "receptor_id", "receptor_id__in", "score__gte", "score__lte", "status",
}
# What makes a row unique inside a key: the newest wins when a pair is docked again.
UNIQUE_BY = ("shard_id", "ligand_id")


def _safe_component(value: object, *, fallback: str) -> str:
    text = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value or ""))
    return text[:64] or fallback


def result_fragment_path(output_dir: str | Path, payload: Mapping[str, Any], shard: Mapping[str, Any]) -> Path:
    """One task's result rows: in its key's folder, the one `compact_tables` merges into a file,
    under a name no other attempt of the same pair can take."""
    from uuid import uuid4

    key = _safe_component(payload.get("state_key"), fallback="default")
    shard_id = int(shard.get("shard_id") or 0)
    return Path(output_dir) / "results" / key / f"shard_{shard_id}-{uuid4().hex[:8]}.results.parquet"


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


def result_row(row: Mapping[str, Any], *, smiles: str, engine: str = "", shard_id: Any = 0,
               docking_run_id: Any = 0) -> dict[str, Any]:
    """One engine row as a parquet row; `metrics_json` holds the result columns past the score."""
    pose_path = local_pose_path(row.get("pose_path"))
    pose_text = "" if pose_path is None else pose_path.read_text(encoding="utf-8", errors="replace")
    pose_index = (row.get("extra") or {}).get("pose_index")
    if pose_path is not None and pose_path.suffix.lower() == ".sdf" and pose_index is not None:
        from amdockvs.docking.rescoring import sdf_records

        pose_text = sdf_records(pose_text)[int(pose_index)]
    metrics = {key: row.get(key) for key in (*SCORE_METRICS, "rmsd_vs_reference", "error", "extra")}
    return {
        "ligand_id": int(row.get("ligand_molecule_id") or 0),
        "shard_id": int(shard_id or 0),
        "docking_run_id": int(docking_run_id or 0),
        "receptor_id": int(row.get("receptor_molecule_id") or 0),
        "score": None if row.get("score") is None else float(row["score"]),
        "score_type": str(row.get("score_type") or ""),
        "status": "failed" if row.get("error") else "done",
        "engine": str(engine),
        "has_pose": pose_path is not None,
        "smiles": str(smiles or ""),
        "metrics_json": json.dumps(metrics, ensure_ascii=True, default=str),
        "pose_text": pose_text,
        "pose_suffix": ".pose" if pose_path is None else pose_path.suffix.lower(),
    }


ROW_GROUP_BYTES = 16 << 20  # pose text buffered per row group: bounds a worker's memory
# Rows per row group when a key's files are merged: pose text is most of a row.
COMPACT_ROWS_PER_GROUP = 4096


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
        # Statistics on `ligand_id` only: a min/max over pose text is most of the footer and
        # nothing filters on it (pose lookups prune row groups by ligand id).
        self._writer = pq.ParquetWriter(self.temporary, schema, compression="zstd", write_statistics=["ligand_id"])
        self._schema = schema
        self._rows: list[dict[str, Any]] = []
        self._bytes = 0

    def add(self, row: Mapping[str, Any]) -> None:
        # Rows are buffered into big row groups: one per ligand made a fragment ~48x larger and
        # ranking ~100x slower (test/benchmarks/results/shard_pipeline_2026-11-18.md). The
        # pose text is already in memory, so the loose pose file is still disposable at once;
        # durability is unchanged, the fragment is published only on success anyway.
        payload = dict(row)
        payload.setdefault("has_pose", bool(payload.get("pose_text")))
        payload.setdefault("shard_id", 0)
        payload.setdefault("docking_run_id", 0)
        self._rows.append(payload)
        self._bytes += len(payload.get("pose_text") or "")
        if self._bytes >= ROW_GROUP_BYTES:
            self._flush()

    def _flush(self) -> None:
        if self._rows:
            self._writer.write_table(pa.Table.from_pylist(self._rows, schema=self._schema))
            self._rows, self._bytes = [], 0

    def __enter__(self) -> "ResultFragmentWriter":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if exc_type is None:
            self._flush()
        self._writer.close()
        if exc_type is None:
            self.temporary.replace(self.path)
        else:
            self.temporary.unlink(missing_ok=True)


def result_metadata(path: str | Path) -> dict[str, Any]:
    raw = (pq.ParquetFile(Path(path)).schema_arrow.metadata or {}).get(b"amdock.metadata", b"{}")
    return dict(json.loads(raw.decode("utf-8")))


def key_files(project_db, keys: Iterable[str]) -> list[Path]:
    """The result files of `keys`, each read in the generation its done-set is of: a key marked
    again under a new library starts over, and its files of the old one are not its rows."""
    from ms_flow.core.data import catalog, states

    wanted = set(keys)
    if not wanted:
        return []
    generations = {row["key"]: row["generation_id"] for row in states.progress(project_db, DONE_KIND)
                   if row["key"] in wanted}
    paths: list[Path] = []
    for key in sorted(wanted):
        spec = {"role": result_role(key)}
        if generations.get(key) is not None:
            spec["generation_id"] = int(generations[key])
        paths.extend(Path(row["path"]) for row in catalog.files(project_db, catalog.shard_spec(**spec))
                     if row["status"] in catalog.READABLE and Path(row["path"]).is_file())
    return paths


def compact_keys(project_db, keys: Iterable[str]) -> None:
    """One file per key, the newest row per (shard, ligand): a redo replaces, a replay is one."""
    from ms_flow.core.data import catalog

    for key in sorted(set(keys)):
        catalog.compact_tables(project_db, result_role(key), unique_by=UNIQUE_BY, stats=["ligand_id"],
                               rows_per_group=COMPACT_ROWS_PER_GROUP)


def _arrow_filters(filters: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    result: list[tuple[str, str, Any]] = []
    for key, value in filters.items():
        if key in ("receptor_id__in", "docking_run_id__in"):
            result.append((key[:-4], "in", [int(item) for item in value]))
        elif key == "score__lte":
            result.append(("score", "<=", float(value)))
        elif key == "score__gte":
            result.append(("score", ">=", float(value)))
        elif key in {"receptor_id", "engine", "status"}:
            result.append((key, "=", int(value) if key == "receptor_id" else str(value)))
    return result


def _summary_rows(path: Path, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    return pq.read_table(
        path,
        columns=list(SUMMARY_COLUMNS),
        filters=_arrow_filters(filters) or None,
    ).to_pylist()


def _row_key(row: Mapping[str, Any]) -> tuple[int, int, int]:
    return int(row.get("shard_id") or 0), int(row.get("ligand_id") or 0), int(row.get("receptor_id") or 0)


def _selected_payloads(path: Path, keys: set[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict[str, Any]]:
    """The full rows of `keys` ((shard id, ligand id, receptor id)) in one file."""
    ligand_ids = sorted({key[1] for key in keys})
    rows = pq.read_table(
        path,
        columns=list(FULL_COLUMNS),
        filters=[("ligand_id", "in", ligand_ids)],
    ).to_pylist()
    return {_row_key(row): row for row in rows if _row_key(row) in keys}


def _pose(payload: Mapping[str, Any]) -> tuple[str, str]:
    suffix = str(payload.get("pose_suffix") or ".pose").lower()
    if not suffix.startswith(".") or not suffix[1:].isalnum():
        suffix = ".pose"
    return str(payload.get("pose_text") or ""), suffix


def select_hit_refs(
    project_db,
    *,
    keys: Iterable[str],
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank the result rows of `keys` using only lightweight columns; shard/record identities."""
    from amdockvs.screening.materialize import TopNGate

    cap = int(top_n)
    if cap <= 0:
        raise ValueError("top_n must be greater than zero.")
    normalized_filters = {"status": "done", **dict(filters or {})}
    unsupported = set(normalized_filters) - SUPPORTED_FILTERS
    if unsupported:
        raise ValueError(f"Unsupported result filters: {', '.join(sorted(unsupported))}.")

    gate = TopNGate(cap=cap, threshold=None)
    for path in key_files(project_db, keys):
        gate.take(
            {**row, "_result_shard": str(path), "_result_record_id": int(row.get("ligand_id") or 0),
             "_shard_id": int(row.get("shard_id") or 0)}
            for row in _summary_rows(path, normalized_filters)
            if row.get("score") is not None and bool(row.get("has_pose"))
        )
    return gate.drain()


def select_hits(
    project_db,
    *,
    keys: Iterable[str],
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank projected score columns, then load poses only for the selected rows."""
    from ms_flow.core.data import catalog

    from amdockvs.io.batches import library_names  # SQL: kept off the worker's import path

    selected = select_hit_refs(project_db, keys=keys, filters=filters, top_n=top_n)
    keys_by_path: dict[Path, set[tuple[int, int, int]]] = {}
    for row in selected:
        keys_by_path.setdefault(Path(row["_result_shard"]), set()).add(_row_key(row))
    payloads = {path: _selected_payloads(path, wanted) for path, wanted in keys_by_path.items()}
    metadata = {path: result_metadata(path) for path in keys_by_path}
    # The shard each row is of, any generation: a row whose shard's file is gone is left out.
    shards = {shard_id: catalog.get_shard(project_db, shard_id)
              for shard_id in {int(row["_shard_id"]) for row in selected}}
    libraries = library_names(project_db) if selected else {}

    hits: list[dict[str, Any]] = []
    for row in selected:
        path = Path(row["_result_shard"])
        payload = payloads.get(path, {}).get(_row_key(row))
        source_shard = shards.get(int(row["_shard_id"]))
        if payload is None or source_shard is None:
            continue
        pose_text, pose_suffix = _pose(payload)
        ligand_id = int(payload.get("ligand_id") or 0)
        hits.append({
            **json.loads(payload.get("metrics_json") or "{}"),
            "docking_run_id": int(payload.get("docking_run_id") or 0),
            "name": f"{libraries.get(source_shard['scope'], 'library')}:{ligand_id}",
            "import_batch_id": source_shard["scope"],
            "source_index": ligand_id,
            "smiles": str(payload.get("smiles") or ""),
            "score": float(payload["score"]),
            "score_type": str(payload.get("score_type") or ""),
            "pose_rank": 1,
            "receptor_molecule_id": int(payload.get("receptor_id") or 0),
            # The key's (one receptor, one box).
            "input_key": str(metadata[path].get("input_key") or ""),
            "_pose_text": pose_text,
            "_pose_suffix": pose_suffix,
            "_result_shard": str(path),
            "_result_record_id": ligand_id,
            "_shard_id": int(row["_shard_id"]),
        })
    return hits


def docked_pairs(project_db, keys: Iterable[str]) -> set[tuple[int, int]]:
    """(docking run id, shard id) of every row the files of `keys` hold: which run docked what."""
    pairs: set[tuple[int, int]] = set()
    for path in key_files(project_db, keys):
        table = pq.read_table(path, columns=["docking_run_id", "shard_id"]).group_by(
            ["docking_run_id", "shard_id"]).aggregate([])
        pairs.update(zip(map(int, table["docking_run_id"].to_pylist()), map(int, table["shard_id"].to_pylist())))
    return pairs


__all__ = [
    "DONE_KIND",
    "RESULT_DATASET_FORMAT",
    "ResultFragmentWriter",
    "compact_keys",
    "docked_pairs",
    "key_files",
    "local_pose_path",
    "off_target_selection",
    "result_fragment_path",
    "result_metadata",
    "result_role",
    "result_row",
    "select_hit_refs",
    "select_hits",
    "state_key",
]
