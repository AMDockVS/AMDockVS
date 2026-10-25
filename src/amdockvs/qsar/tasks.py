"""What a worker runs to calculate the descriptors of a batch of molecules.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from typing import Any

from amdockvs.chemistry.descriptors import calculate_alert_rows, calculate_descriptor_rows
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS
from amdockvs.core.jobs import task
from amdockvs.io.rows import identity_representations


# Descriptor values are the source of truth on MoleculeRecord columns (the separate
# DescriptorVectorRecord BLOB is unused/incompatible here). Persistence goes through a
# MolSuite sink (engine-side, upsert), NOT a direct worker DB write — a second SQLite
# connection from a process/thread worker deadlocks against the engine's connection.
_DESCRIPTOR_COLUMNS = (
    "mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds",
    "fragment_count", "ring_count", "aromatic_ring_count", "hetero_atom_count",
    "heavy_atom_count", "formal_charge", "fraction_csp3",
)


@task(
    name="amdock_calculate_descriptor_batch",
    description="Calculate RDKit descriptors for one ligand batch.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def calculate_descriptor_batch(payload: dict):
    # The worker gets each molecule's bytes in its item and COMPUTES; it returns rows for the
    # engine-side sink to persist. It does not open a DB connection.
    rows = calculate_descriptor_rows(list(payload.get("items") or []), fingerprint=payload.get("fp"))
    return _descriptor_graph_payload(rows)


_ALERT_COLUMNS = ("pains_matches",)


@task(
    name="amdock_calculate_alerts_batch",
    description="Calculate PAINS alerts and identity keys for one ligand batch.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def calculate_alerts_batch(payload: dict):
    rows = calculate_alert_rows(list(payload.get("items") or []))
    return {
        "molecules": [{"id": row["id"], "pains_matches": row["pains_matches"]} for row in rows],
        "molecule_representations": [r for row in rows for r in identity_representations(row, molecule_id=row["id"])],
    }


def _descriptor_graph_payload(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Shape descriptor rows for DESCRIPTOR_GRAPH_OUTPUT: descriptor values upserted onto
    MoleculeRecord columns, optional Morgan fingerprints upserted into FingerprintRecord."""
    molecules: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "") != "completed":
            continue
        molecule_id = int(row.get("molecule_id") or 0)
        molecules.append({"id": molecule_id, **{col: row.get(col) for col in _DESCRIPTOR_COLUMNS}})
        if row.get("fp_binary"):
            fingerprints.append({
                "molecule_id": molecule_id,
                "fp_type": row["fp_type"],
                "nbits": int(row["fp_nbits"]),
                "radius": int(row["fp_radius"]),
                "fp_binary": row["fp_binary"],
            })
    return {"molecules": molecules, "fingerprints": fingerprints}
