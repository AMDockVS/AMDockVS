"""Project inventory access for the generic MolSuite shard container."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ms_flow.core.data import catalog
from ms_flow.core.data.shard import Shard

from amdockvs.io.shards import shard_file_name, shard_number


@dataclass
class ShardAPI:
    runtime: Any

    def get(self, shard_id: int) -> dict[str, Any] | None:
        """One shard (a row group of a catalog file) by its id, from any generation or role."""
        self.runtime._require_active_project()
        return catalog.get_shard(self.runtime.workspace.project_db, int(shard_id))

    def open(self, shard_id: int) -> Shard:
        """Open a registered shard; use the returned object as a context manager."""
        row = self.get(shard_id)
        if row is None:
            raise ValueError(f"Screening shard {shard_id} does not exist.")
        path = Path(str(row["path"] or "")).expanduser()
        if not path.is_absolute():
            path = Path(self.runtime.workspace.active_context.path) / path
        if not path.is_file():
            raise FileNotFoundError(path)
        return Shard.open(path, shard=shard_number(row))


__all__ = ["ShardAPI", "shard_file_name", "shard_number"]
