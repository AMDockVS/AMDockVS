"""ms_flow owns every storage write (M7b): the app writes no Parquet or Arrow IPC of its own,
builds no shard container and makes no catalog row outside ms_flow's writers."""
import re
from pathlib import Path

import amdockvs

DENIED = re.compile(
    r"pq\.write_table|ParquetWriter|\.to_parquet\(|write_to_dataset|write_feather"
    r"|pyarrow\.ipc|ipc\.new_(file|stream)|RecordBatch(File|Stream)Writer"
    r"|\bShardWriter\b|\b(add_file|update_file|finish_file)\("
)


def test_the_app_writes_no_storage_format_of_its_own():
    root = Path(amdockvs.__file__).parent
    found = [f"{path.relative_to(root)}:{n}: {line.strip()}"
             for path in sorted(root.rglob("*.py"))
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if DENIED.search(line)]
    assert found == []
