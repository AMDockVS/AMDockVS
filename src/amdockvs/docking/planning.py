"""Typed planning models shared by Docking Studio and headless callers."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.docking.protocols import PROTOCOL_SCHEMA, protocol_hash
from amdockvs.project.sets import MoleculeSetRef


@dataclass(frozen=True)
class DockingProtocol:
    """One scientific docking protocol plus its execution configuration."""

    program: str
    label: str
    config: Mapping[str, Any] = field(default_factory=dict)
    rescoring: tuple[Mapping[str, Any], ...] = ()
    hash: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DockingProtocol":
        program = str(value.get("program") or "").strip()
        config = dict(value.get("config") or {})
        rescoring = tuple(dict(item) for item in (value.get("rescoring") or ()))
        resolved_hash = str(value.get("hash") or "") or protocol_hash(
            program=program,
            config=config,
            rescoring=list(rescoring),
        )
        return cls(
            program=program,
            label=str(value.get("label") or program),
            config=config,
            rescoring=rescoring,
            hash=resolved_hash,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": PROTOCOL_SCHEMA,
            "program": self.program,
            "label": self.label,
            "config": dict(self.config),
            "rescoring": [dict(item) for item in self.rescoring],
            "hash": self.hash,
        }


MoleculeSource = MoleculeScope | MoleculeSetRef


def program_run_id(click_id: str, protocol_hash: str, index: int, programs: int) -> str:
    """The `run_id` of one program's job of a click over sharded libraries: the click's own when
    it runs one program, else the click's with the protocol's hash (or its place) after it, so
    each program's campaign keeps its own records. `click_id` undoes it."""
    if programs <= 1:
        return str(click_id)
    return f"{click_id}-{str(protocol_hash)[:12] or index + 1}"


_PROGRAM_RUN_ID = re.compile(r"^([0-9a-f]{32})-(?:[0-9a-f]{12}|\d+)$")


def click_id(run_id: str) -> str:
    """The Run click `run_id` belongs to: `program_run_id` undone. Only that exact shape (the
    click's 32-hex id and its suffix) is undone, so a `run_id` an API caller chose is its own."""
    match = _PROGRAM_RUN_ID.match(str(run_id or ""))
    return match.group(1) if match else str(run_id or "")


@dataclass(frozen=True)
class DockingRunRequest:
    """Complete immutable request passed from the Qt layer to submission."""

    run_kind: str
    ligand_scope: MoleculeSource | None
    receptor_scope: MoleculeSource | None
    protocols: tuple[DockingProtocol, ...]
    skip_existing: bool = True
    batch_size: int | None = None  # None -> each program's pairs per task (Settings)
    executor_name: str = "thread"
    run_id: str = ""
    compute_interactions: bool = False
    complex_ids: tuple[int, ...] = ()
    # Off-target funnel: dock the whole ligand scope against this receptor, then the other
    # receptors only against its `offtarget_top_n` best ligands.
    offtarget_reference_id: int | None = None
    offtarget_top_n: int = 0
    offtarget_max_score: float | None = None  # by-threshold selection; top_n is then the ceiling
    replace_existing: bool = False


@dataclass(frozen=True)
class DockingRunIdentity:
    """In-memory scope identity used to prevent duplicate live submissions."""

    receptor_type: str
    ligand_type: str
    ligand_mode: str
    ligand_ids: tuple[int, ...]
    receptor_mode: str
    receptor_ids: tuple[int, ...]


def docking_signature(request: DockingRunRequest, identity: DockingRunIdentity) -> str:
    payload = {
        "protocols": sorted(
            ({
                "program": protocol.program,
                "hash": protocol.hash,
                "config": dict(protocol.config),
                "rescoring": [dict(item) for item in protocol.rescoring],
            } for protocol in request.protocols),
            key=lambda item: (item["program"], item["hash"]),
        ),
        "run_kind": request.run_kind or "docking",
        "receptor_type": identity.receptor_type,
        "ligand_type": identity.ligand_type,
        "lig_mode": identity.ligand_mode,
        "sel_lig": list(identity.ligand_ids) if identity.ligand_mode == "selected" else "all",
        "rec_mode": identity.receptor_mode,
        "sel_rec": list(identity.receptor_ids) if identity.receptor_mode == "selected" else "all",
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def protocol_job_key(protocol: DockingProtocol, index: int) -> str:
    label = str(protocol.label or protocol.program).replace(" ", "_")
    suffix = str(protocol.hash or "")[:8]
    return f"{index + 1}:{label}:{suffix}" if suffix else f"{index + 1}:{label}"


__all__ = [
    "DockingProtocol",
    "DockingRunIdentity",
    "DockingRunRequest",
    "MoleculeSource",
    "docking_signature",
    "protocol_job_key",
]
