from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


PROTOCOL_SCHEMA = "amdockvs.protocol.v1"


class DockingProtocolMetadata(BaseModel):
    """Result metadata that identifies a scientific protocol variant.

    Engine execution params stay as first-class job params. This object is only the
    descriptor used for audit, result grouping, and protocol-aware skip/re-run logic.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=PROTOCOL_SCHEMA, alias="schema")
    program: str = ""
    label: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    rescoring: list[dict[str, Any]] = Field(default_factory=list)
    hash: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "DockingProtocolMetadata":
        payload = dict(value or {})
        metadata = cls(**payload)
        if not metadata.hash and (metadata.program or metadata.config or metadata.rescoring):
            metadata.hash = protocol_hash(
                program=metadata.program,
                config=metadata.config,
                rescoring=metadata.rescoring,
            )
        return metadata

    def as_metrics_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", by_alias=True)


#: Search and output settings distinguish results; CPU/backend settings remain execution details.
IDENTITY_KEYS = ("scoring_function", "exhaustiveness")
RESULT_KEYS = ("num_modes", "seed", "energy_range", "min_rmsd", "spacing")


def protocol_identity(config: Mapping[str, Any] | None, *, program: str = "") -> dict[str, Any]:
    """The scientific subset of `config` — what the hash and the label are built from."""
    payload = dict(config or {})
    keys = IDENTITY_KEYS
    if program:
        from amdockvs.docking.engines.programs import get_docking_program

        keys = get_docking_program(program).protocol_identity_keys
    identity = {key: payload[key] for key in (*keys, *RESULT_KEYS) if key in payload}
    # Standard is what every protocol was before methods existed: leaving it out keeps their hashes.
    if str(payload.get("method") or "standard") != "standard":
        identity["method"] = str(payload["method"])
    return identity


def protocol_hash(*, program: str, config: Mapping[str, Any], rescoring: list[dict[str, Any]] | None = None) -> str:
    payload = {
        "program": str(program),
        "config": protocol_identity(config, program=program),
        "rescoring": list(rescoring or []),
        "schema": PROTOCOL_SCHEMA,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


__all__ = [
    "DockingProtocolMetadata",
    "IDENTITY_KEYS",
    "PROTOCOL_SCHEMA",
    "protocol_hash",
    "protocol_identity",
]
