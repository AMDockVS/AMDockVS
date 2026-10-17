from __future__ import annotations

import io
from typing import Any

from amdockvs.core.payloads import payload_store


def read_source_properties(project: Any, payload_id: int | None, source_index: int) -> dict[str, str]:
    """Load one molecule's imported SDF tags from its batch's parquet payload
    (molecules.properties_payload_id, written by offload_source_properties).

    Filtered by source_index, so the batch's other molecules are skipped. Returns {} if the
    molecule had no tags — callers treat missing properties as "none", not an error.
    """
    if not payload_id:
        return {}

    import pyarrow.parquet as pq

    table = pq.read_table(
        io.BytesIO(payload_store(project).get(int(payload_id))),
        columns=["key", "value"],
        filters=[("source_index", "=", int(source_index))],
    )
    return dict(zip(map(str, table.column("key").to_pylist()), map(str, table.column("value").to_pylist())))


__all__ = ["read_source_properties"]
