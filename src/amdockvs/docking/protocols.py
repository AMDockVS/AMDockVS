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


#: Config keys that make a *different protocol*. Everything else the UI puts in `config` is
#: execution detail -- how many poses to report (`num_modes`), which vina binding
#: (`vina_backend`), how many threads (`vina_cpu`) -- and it stays in the stored config for
#: audit, but it must not fork the protocol: hashing it split the Results filter and defeated
#: the skip-existing guard on changes that cannot move a single pose.
IDENTITY_KEYS = ("scoring_function", "exhaustiveness")


def protocol_identity(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The scientific subset of `config` — what the hash and the label are built from."""
    payload = dict(config or {})
    return {key: payload[key] for key in IDENTITY_KEYS if key in payload}


def protocol_hash(*, program: str, config: Mapping[str, Any], rescoring: list[dict[str, Any]] | None = None) -> str:
    payload = {
        "program": str(program),
        "config": protocol_identity(config),
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
