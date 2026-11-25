"""Durable, columnar results of sharded docking campaigns.

What is docked lives in ms_flow, not in a table here: a done-set of shard ids per key
(`ms_flow.core.data.states`, kind `DONE_KIND`) and the result rows in two containers of the
catalog per key: the summary (`summary_role`, what is ranked) and the payload (`payload_role`,
poses and the rest), one shard of each per docked shard. A key is one target: what a shard is
docked against, and under which selection.

The worker that docks a shard writes the files, so what reads the project database (the ranking,
the hits) imports SQL where it runs, not here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pyarrow as pa

from amdockvs.core.paths import get_default_project_root
from amdockvs.docking.results.metrics import SCORE_METRICS

RESULT_DATASET_FORMAT = "amdock-docking-results-arrows-v1"
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


def summary_role(key: str) -> str:
    """The catalog role of a key's ranked columns (`SUMMARY_SCHEMA`): what a summary read opens."""
    return f"summary:{key}"


def payload_role(key: str) -> str:
    """The catalog role of the rest of a key's rows (`PAYLOAD_SCHEMA`): the poses."""
    return f"payload:{key}"


def result_roles(key: str) -> tuple[str, str]:
    """Both roles of a key, payload first: the order they are committed in (`PairWriter`)."""
    return payload_role(key), summary_role(key)


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
SUMMARY_SCHEMA = pa.schema([RESULT_SCHEMA.field(name) for name in SUMMARY_COLUMNS])
# The keys again, then what only a hit needs: read for the few the ranking picks.
PAYLOAD_COLUMNS = ("ligand_id", "shard_id", "receptor_id", "smiles", "metrics_json", "pose_text", "pose_suffix")
PAYLOAD_SCHEMA = pa.schema([RESULT_SCHEMA.field(name) for name in PAYLOAD_COLUMNS])
SUPPORTED_FILTERS = {
    "docking_run_id__in", "engine", "receptor_id", "receptor_id__in", "score__gte", "score__lte", "status",
}
# What makes a row unique inside a key: the newest wins when a pair is docked again.
UNIQUE_BY = ("shard_id", "ligand_id")


def _safe_component(value: object, *, fallback: str) -> str:
    text = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value or ""))
    return text[:64] or fallback


def result_fragment_path(output_dir: str | Path, payload: Mapping[str, Any], shard: Mapping[str, Any]) -> Path:
    """One task's summary part (`payload_part` of it is the other): in its key's parts folder, the
    key's containers one up, under a name no other attempt of the same pair can take."""
    from uuid import uuid4

    key = _safe_component(payload.get("state_key"), fallback="default")
    shard_id = int(shard.get("shard_id") or 0)
    return Path(output_dir) / "results" / key / "parts" / f"shard_{shard_id}-{uuid4().hex[:8]}.arrows"


def payload_part(path: str | Path) -> Path:
    """The payload part beside a summary part: `X.arrows` gives `X.payload.arrows`."""
    path = Path(path)
    return path.with_name(path.name.removesuffix(".arrows") + ".payload.arrows")


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


class ResultFragmentWriter:
    """A task's rows, buffered, then its two part containers on success (`write_table`): the summary
    at `path`, `metadata` (the run's facts) as its shard's meta, and the payload beside it. Nothing is
    left on failure. ponytail: a shard's rows held at once (256 poses); stream batches if shards grow."""

    def __init__(self, path: str | Path, *, metadata: Mapping[str, Any], origin: int | None = None,
                 base_id: int = 0, slot_count: int | None = None) -> None:
        self.path = Path(path)
        self.metadata = {"record_format": RESULT_DATASET_FORMAT, **dict(metadata)}
        # The library shard docked and its id range: what a newer shard of the same origin replaces.
        self._shard = {"origin": origin, "base_id": int(base_id or 0), "slot_count": slot_count}
        self._rows: list[dict[str, Any]] = []

    def add(self, row: Mapping[str, Any]) -> None:
        payload = dict(row)
        payload.setdefault("has_pose", bool(payload.get("pose_text")))
        payload.setdefault("shard_id", 0)
        payload.setdefault("docking_run_id", 0)
        self._rows.append(payload)

    def __enter__(self) -> "ResultFragmentWriter":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if exc_type is not None:
            return
        from ms_flow.core.data.shard import write_table

        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_table(payload_part(self.path), pa.Table.from_pylist(self._rows, schema=PAYLOAD_SCHEMA))
        write_table(self.path, pa.Table.from_pylist(self._rows, schema=SUMMARY_SCHEMA), meta=self.metadata,
                    **self._shard)


def result_metadata(path: str | Path) -> dict[str, Any]:
    """The run's facts a summary container (or part) holds: its first live shard's meta."""
    from ms_flow.core.data.shard import file_groups

    return next((dict(json.loads(group.meta or "{}")) for group in file_groups(path) if not group.retired), {})


def key_files(project_db, keys: Iterable[str], *, role=summary_role) -> list[Path]:
    """The containers of `keys` in `role(key)` (the summaries by default), oldest first per key."""
    return [path for _key, _file_id, path in _key_containers(project_db, keys, role=role)]


def _key_containers(project_db, keys: Iterable[str], *, role=summary_role) -> list[tuple[str, int, Path]]:
    """(key, file id, path) of the containers of `keys` in `role(key)`, oldest first, each read in
    the generation its done-set is of: a key marked again under a new library starts over, and its
    files of the old one are not its rows."""
    from ms_flow.core.data import catalog, states

    wanted = set(keys)
    if not wanted:
        return []
    generations = {row["key"]: row["generation_id"] for row in states.progress(project_db, DONE_KIND)
                   if row["key"] in wanted}
    found: list[tuple[str, int, Path]] = []
    for key in sorted(wanted):
        spec = {"role": role(key)}
        if generations.get(key) is not None:
            spec["generation_id"] = int(generations[key])
        found.extend(sorted((key, int(row["id"]), Path(row["path"]))
                            for row in catalog.files(project_db, catalog.shard_spec(**spec))
                            if row["status"] in catalog.READABLE and Path(row["path"]).is_file()))
    return found


def _newest(table: pa.Table) -> pa.Table:
    """The last row per `UNIQUE_BY`, rows given oldest first: a redo replaces, a replay is one."""
    import numpy as np

    at = table.append_column("_at", pa.array(np.arange(table.num_rows)))
    last = at.group_by(list(UNIQUE_BY), use_threads=False).aggregate([("_at", "max")])["_at_max"]
    return table.take(np.sort(last.to_numpy()))


def _summaries(project_db, keys: Iterable[str], columns: Iterable[str] = SUMMARY_COLUMNS, *,
               newest: bool = True) -> tuple[pa.Table, dict[int, dict[str, Any]]]:
    """The summary rows of `keys` as one table, `_result_shard` (the shard id of each) beside them,
    and the meta of every shard read. `newest`: one row per (shard, ligand) of a key, the last
    committed; only a key with a library shard docked twice (an origin repeated) is deduplicated."""
    import numpy as np

    from ms_flow.core.data.catalog import shard_id
    from ms_flow.core.data.shard import file_groups, read_rows

    names = list(dict.fromkeys([*columns, *UNIQUE_BY]))
    metas: dict[int, dict[str, Any]] = {}
    by_key: dict[str, list[tuple[pa.Table, list]]] = {}
    for key, file_id, path in _key_containers(project_db, keys):
        groups = [group for group in file_groups(path) if not group.retired]
        if not groups:
            continue
        ids = [shard_id(file_id, group.shard) for group in groups]
        metas.update(zip(ids, (dict(json.loads(group.meta or "{}")) for group in groups)))
        sizes = [group.n_records + group.n_dropped for group in groups]
        table = read_rows(path, [group.shard for group in groups], names)
        by_key.setdefault(key, []).append((table.append_column(
            "_result_shard", pa.array(np.repeat(ids, sizes), pa.int64())), [group.origin for group in groups]))
    tables = []
    for parts in by_key.values():
        table = pa.concat_tables([table for table, _origins in parts])
        origins = [origin for _table, found in parts for origin in found]
        tables.append(_newest(table) if newest and len(origins) != len(set(origins)) else table)
    if not tables:
        empty = pa.schema([RESULT_SCHEMA.field(name) for name in names]).empty_table()
        return empty.append_column("_result_shard", pa.array([], pa.int64())), metas
    return pa.concat_tables(tables), metas


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


def _filtered(table: pa.Table, filters: Mapping[str, Any]) -> pa.Table:
    import pyarrow.parquet as pq

    found = _arrow_filters(filters)
    return table.filter(pq.filters_to_expression(found)) if found else table


def _summary_table(path: Path, filters: Mapping[str, Any]) -> pa.Table:
    """The summary rows of one part container (a stage on a cluster ranks its own)."""
    from ms_flow.core.data.shard import read_rows

    return _filtered(read_rows(path, None, list(SUMMARY_COLUMNS)), filters)


def _row_key(row: Mapping[str, Any]) -> tuple[int, int, int]:
    return int(row.get("shard_id") or 0), int(row.get("ligand_id") or 0), int(row.get("receptor_id") or 0)


def _selected_payloads(path: Path, shards: Iterable[int] | None,
                       keys: set[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict[str, Any]]:
    """The payload rows of `keys` ((shard id, ligand id, receptor id)) in shard numbers `shards` of
    one container (every live one with None)."""
    import pyarrow.compute as pc

    from ms_flow.core.data.shard import read_rows

    table = read_rows(path, shards, list(PAYLOAD_COLUMNS))
    table = table.filter(pc.is_in(table["ligand_id"], value_set=pa.array(sorted({key[1] for key in keys}), pa.int64())))
    return {_row_key(row): row for row in table.to_pylist() if _row_key(row) in keys}


def _linked_payloads(project_db, links: Iterable[int],
                     keys: set[tuple[int, int, int]]) -> dict[tuple[int, int, int], dict[str, Any]]:
    """The payload rows of `keys` in the payload shards `links` (a summary shard's `meta["bulk"]`:
    those of its own attempt)."""
    from ms_flow.core.data import catalog

    by_file: dict[int, list[int]] = {}
    for link in links:
        file_id, number = catalog.split_shard_id(int(link))
        by_file.setdefault(file_id, []).append(number)
    found: dict[tuple[int, int, int], dict[str, Any]] = {}
    for file_id, numbers in by_file.items():
        shard = catalog.get_shard(project_db, catalog.shard_id(file_id, numbers[0]))
        if shard is not None:  # its file is gone: its rows are left out
            found.update(_selected_payloads(Path(shard["path"]), numbers, keys))
    return found


def _pose(payload: Mapping[str, Any]) -> tuple[str, str]:
    suffix = str(payload.get("pose_suffix") or ".pose").lower()
    if not suffix.startswith(".") or not suffix[1:].isalnum():
        suffix = ".pose"
    return str(payload.get("pose_text") or ""), suffix


def _top(table: pa.Table, cap: int) -> pa.Table:
    """The best `cap` scored rows with a pose, in Arrow: lowest score first, a tie to the later
    row (the order `TopNGate` gives)."""
    import numpy as np
    import pyarrow.compute as pc

    table = table.filter(pc.and_(pc.is_valid(table["score"]), pc.fill_null(table["has_pose"], False)))
    at = table.append_column("_at", pa.array(np.arange(table.num_rows)))
    order = pc.sort_indices(at, sort_keys=[("score", "ascending"), ("_at", "descending")])
    return table.take(order[:cap])


def _ranked(project_db, *, keys: Iterable[str], filters: Mapping[str, Any] | None,
            top_n: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    cap = int(top_n)
    if cap <= 0:
        raise ValueError("top_n must be greater than zero.")
    normalized_filters = {"status": "done", **dict(filters or {})}
    unsupported = set(normalized_filters) - SUPPORTED_FILTERS
    if unsupported:
        raise ValueError(f"Unsupported result filters: {', '.join(sorted(unsupported))}.")

    table, metas = _summaries(project_db, keys)  # newest first, then the filters (a redo hides the old row)
    return [{**row, "_result_record_id": int(row.get("ligand_id") or 0), "_shard_id": int(row.get("shard_id") or 0)}
            for row in _top(_filtered(table, normalized_filters), cap).to_pylist()], metas


def select_hit_refs(
    project_db,
    *,
    keys: Iterable[str],
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank the result rows of `keys` from their summaries alone: shard/record identities, and
    `_result_shard` (the summary shard of each)."""
    return _ranked(project_db, keys=keys, filters=filters, top_n=top_n)[0]


def select_hits(
    project_db,
    *,
    keys: Iterable[str],
    filters: Mapping[str, Any] | None = None,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank the summaries, then load poses only for the selected rows, from the payload of each
    one's own attempt."""
    from ms_flow.core.data import catalog

    from amdockvs.io.batches import library_names  # SQL: kept off the worker's import path

    selected, metas = _ranked(project_db, keys=keys, filters=filters, top_n=top_n)
    by_summary: dict[int, set[tuple[int, int, int]]] = {}
    for row in selected:
        by_summary.setdefault(int(row["_result_shard"]), set()).add(_row_key(row))
    payloads = {summary: _linked_payloads(project_db, metas[summary].get("bulk") or (), wanted)
                for summary, wanted in by_summary.items()}
    # The shard each row is of, any generation: a row whose shard's file is gone is left out.
    shards = {shard_id: catalog.get_shard(project_db, shard_id)
              for shard_id in {int(row["_shard_id"]) for row in selected}}
    libraries = library_names(project_db) if selected else {}

    hits: list[dict[str, Any]] = []
    for row in selected:
        summary = int(row["_result_shard"])
        payload = payloads[summary].get(_row_key(row))
        source_shard = shards.get(int(row["_shard_id"]))
        if payload is None or source_shard is None:
            continue
        pose_text, pose_suffix = _pose(payload)
        ligand_id = int(row.get("ligand_id") or 0)
        hits.append({
            **json.loads(payload.get("metrics_json") or "{}"),
            "docking_run_id": int(row.get("docking_run_id") or 0),
            "name": f"{libraries.get(source_shard['scope'], 'library')}:{ligand_id}",
            "import_batch_id": source_shard["scope"],
            "source_index": ligand_id,
            "smiles": str(payload.get("smiles") or ""),
            "score": float(row["score"]),
            "score_type": str(row.get("score_type") or ""),
            "pose_rank": 1,
            "receptor_molecule_id": int(row.get("receptor_id") or 0),
            # The key's (one receptor, one box).
            "input_key": str(metas[summary].get("input_key") or ""),
            "_pose_text": pose_text,
            "_pose_suffix": pose_suffix,
            "_result_shard": summary,
            "_result_record_id": ligand_id,
            "_shard_id": int(row["_shard_id"]),
        })
    return hits


def docked_pairs(project_db, keys: Iterable[str]) -> set[tuple[int, int]]:
    """(docking run id, shard id) of every row the summaries of `keys` hold, older copies too:
    which run docked what."""
    table, _metas = _summaries(project_db, keys, ("docking_run_id", "shard_id"), newest=False)
    table = table.group_by(["docking_run_id", "shard_id"]).aggregate([])
    return set(zip(map(int, table["docking_run_id"].to_pylist()), map(int, table["shard_id"].to_pylist())))


def run_counts(project_db, keys: Iterable[str], run: int) -> tuple[int, int]:
    """(shards, rows) that `run` holds in the summaries of `keys`, the newest row of each pair."""
    import pyarrow.compute as pc

    table, _metas = _summaries(project_db, keys, ("docking_run_id", "shard_id"))
    table = table.filter(pc.equal(table["docking_run_id"], int(run)))
    return len(set(table["shard_id"].to_pylist())), table.num_rows


__all__ = [
    "DONE_KIND",
    "RESULT_DATASET_FORMAT",
    "ResultFragmentWriter",
    "docked_pairs",
    "key_files",
    "local_pose_path",
    "off_target_selection",
    "payload_part",
    "payload_role",
    "result_fragment_path",
    "result_metadata",
    "result_roles",
    "result_row",
    "run_counts",
    "select_hit_refs",
    "select_hits",
    "state_key",
    "summary_role",
]
