"""A molecule's structure between its payload bytes and an RDKit Mol.

A task gets the bytes from its job's `with_payloads` source; a reader outside a job asks the
project's store. Either way the format is the payload's kind, and `io.formats.read_mol` stays the
one reader: the bytes reach it as a scratch file that is gone when it returns.
"""
from __future__ import annotations

from typing import Any, Iterable

from amdockvs.core.constants import SERIES_MOLECULES
from amdockvs.core.payloads import molecule_payload_id, payload_store, structure_payload


def mol_from_bytes(data: bytes | str | None, kind: str | None, **options: Any):
    """One RDKit Mol from stored bytes in format `kind` ("sdf", "pdb"...), or None."""
    from ms_flow.payloads import scratch_file

    from amdockvs.io.formats import read_mol

    if not data or not kind:
        return None
    raw = data.encode() if isinstance(data, str) else bytes(data)
    with scratch_file(raw, f".{str(kind).lstrip('.').lower()}") as path:
        return read_mol(path, **options)


def molecule_mol(project: Any, row: Any, **options: Any):
    """The structure a molecule row (record or mapping) is now, as an RDKit Mol, or None."""
    payload_id = molecule_payload_id(row)
    if payload_id is None:
        return None
    store = payload_store(project)
    return mol_from_bytes(store.get(payload_id), store.kinds([payload_id]).get(payload_id), **options)


def with_structures(project: Any, rows: Iterable[dict], *, original: bool = False):
    """A job's rows carrying the bytes they work on: `structure` and its format, `structure_kind`.

    The current structure, or with `original` the imported one. Rows are read a window at a time in
    disk order (`ms_flow.payloads.with_payloads`), so a task never sees a path.
    """
    from ms_flow.payloads import with_payloads

    def choose(source: Iterable[dict]):
        for row in source:
            imported = row.get("stored_payload_id") or row.get("current_payload_id")
            yield {**row, "structure_payload_id": imported if original else molecule_payload_id(row)}

    return with_payloads(project, choose(rows), {"structure_payload_id": "structure"}, kinds=True)


def sdf_payload(mol, *, conf_id: int = -1, series: str = SERIES_MOLECULES):
    """An RDKit Mol (one conformer of it) as the SDF payload a Build or an edit writes."""
    from rdkit import Chem

    return structure_payload(Chem.MolToMolBlock(mol, confId=conf_id) + "$$$$\n", "sdf", series)


__all__ = ["mol_from_bytes", "molecule_mol", "sdf_payload", "with_structures"]
