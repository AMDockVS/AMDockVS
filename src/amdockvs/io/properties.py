from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

# The molecule extra_data key recording which parquet shard holds its offloaded SDF
# tags, and the sub-directory (under the molecules storage dir) the shards live in.
# Written by offload_source_properties at import time; read here on demand.
PROPS_SHARD_KEY = "__props_shard"
PROPERTIES_SUBDIR = "properties"


def read_source_properties(
    properties_dir: str | Path,
    shard: str | None,
    source_index: int,
) -> dict[str, str]:
    """Load one molecule's offloaded SDF tags from its parquet shard.

    Filtered by source_index (parquet predicate pushdown), so only the relevant rows
    are read. Returns {} if the molecule's tags were never offloaded or the shard is
    gone — callers treat missing properties as "none", not an error.
    """
    if not shard:
        return {}
    path = Path(properties_dir).expanduser().resolve() / str(shard)
    if not path.exists():
        return {}

    import pyarrow.parquet as pq

    table = pq.read_table(
        path,
        columns=["key", "value"],
        filters=[("source_index", "=", int(source_index))],
    )
    keys = table.column("key").to_pylist()
    values = table.column("value").to_pylist()
    return {str(key): str(value) for key, value in zip(keys, values)}


def properties_from_extra_data(
    extra_data: Mapping[str, Any] | None,
    source_index: int,
    properties_dir: str | Path,
) -> dict[str, str]:
    """Convenience: resolve the shard ref from a molecule's extra_data and read."""
    shard = (extra_data or {}).get(PROPS_SHARD_KEY)
    return read_source_properties(properties_dir, shard, source_index)


__all__ = [
    "PROPS_SHARD_KEY",
    "PROPERTIES_SUBDIR",
    "read_source_properties",
    "properties_from_extra_data",
]
