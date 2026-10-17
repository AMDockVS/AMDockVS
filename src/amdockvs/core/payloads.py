"""Structures as payloads: the bytes live in the project's volumes (ms_flow.payloads), a row keeps
`<role>_payload_id`, and the payload's `kind` is its format ("sdf", "pdb", "pdbqt"...).

A task returns `structure_payload(...)` in its row and the sink stores it; a reader asks the
project's store. No path to a molecule exists anywhere: a program that only reads paths gets
`ms_flow.payloads.scratch_file` or `PayloadStore.materialize`.
"""
from __future__ import annotations

from typing import Any, Mapping

from ms_flow.payloads import Payload

from amdockvs.core.constants import SERIES_MOLECULES


def structure_payload(data: bytes | str, kind: str, series: str = SERIES_MOLECULES) -> Payload:
    """Bytes (or text) of one structure in format `kind` ("sdf", ".PDB"...), as a sink stores them."""
    return Payload(data.encode() if isinstance(data, str) else bytes(data), kind=kind.lstrip(".").lower(),
                   series=series)


def molecule_payload_id(row: Mapping[str, Any] | Any) -> int | None:
    """The structure a molecule is now: its current form, else what was imported."""
    get = row.get if isinstance(row, Mapping) else lambda key: getattr(row, key, None)
    return get("current_payload_id") or get("stored_payload_id") or None


def payload_store(project: Any):
    """The `PayloadStore` of a runtime, workspace, project store or job `project`."""
    from ms_flow.core.database import resolve_project_store

    return resolve_project_store(project).payloads


def read_structure(project: Any, payload_id: int) -> tuple[str, str]:
    """(text, kind) of one stored structure."""
    store = payload_store(project)
    return store.get(payload_id).decode(), store.kinds([payload_id]).get(payload_id, "")


__all__ = ["molecule_payload_id", "payload_store", "read_structure", "structure_payload"]
