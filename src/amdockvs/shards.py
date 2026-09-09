"""Project inventory access for the generic MolSuite shard container."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ms_flow.core.data.shard import Shard

from amdockvs.models import ScreeningShard


@dataclass
class ShardAPI:
    runtime: Any

    def get(self, shard_id: int) -> ScreeningShard | None:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            return session.get(ScreeningShard, int(shard_id))

    def open(self, shard_id: int) -> Shard:
        """Open a registered shard; use the returned object as a context manager."""
        row = self.get(shard_id)
        if row is None:
            raise ValueError(f"Screening shard {shard_id} does not exist.")
        path = Path(str(row.path or "")).expanduser()
        if not path.is_absolute():
            path = Path(self.runtime.molsuite.active_context.path) / path
        if not path.is_file():
            raise FileNotFoundError(path)
        return Shard.open(path)


__all__ = ["ShardAPI"]
