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
DESCRIPTOR_COLUMNS = (
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


def _feature_vector(mol, fitted) -> list[float] | None:
    """What the model was trained on, computed from the molecule: the row path reads the same
    values off the stored fingerprint or the rdkit2d block."""
    import numpy as np

    from amdockvs.chemistry.descriptors import calculate_rdkit2d_descriptors
    from amdockvs.chemistry.fingerprints import morgan_fingerprint

    if fitted.feature_kind == "ecfp4":
        bits = morgan_fingerprint(mol, radius=int(fitted.fp_radius or 2), n_bits=int(fitted.fp_nbits or 2048))
        return list(np.frombuffer(bits.ToBitString().encode("ascii"), dtype=np.uint8) - ord("0"))
    values = calculate_rdkit2d_descriptors(mol)
    return [float(values.get(name, np.nan)) for name in fitted.feature_names] if values else None


@task(
    name="amdock_qsar_predict_shard",
    description="Score one library shard with a QSAR model.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def predict_shard_task(payload: dict) -> dict:
    """One shard's predictions as `<shard id>.parquet` (key, value, confidence), keyed by
    `io.shards.shard_key`: the library is scored without a row of it in the database. A record
    that does not parse, or whose features cannot be computed, is left unscored."""
    from pathlib import Path

    import numpy as np
    import pandas as pd

    from amdockvs.chemistry.shards import read_shard
    from amdockvs.io.shards import shard_key
    from amdockvs.qsar.modeling import load_model

    fitted = load_model(payload["model_path"])
    keys, rows = [], []
    for record_id, mol in zip(*read_shard(payload["shard_path"])):
        try:
            vector = None if mol is None else _feature_vector(mol, fitted)
        except Exception:  # noqa: BLE001 - one odd molecule does not fail its shard
            vector = None
        if vector is not None:
            keys.append(shard_key(payload.get("import_batch_id"), record_id))
            rows.append(vector)
    values = confidence = positive_probability = np.zeros(0)
    if rows:
        x_matrix = np.asarray(rows, dtype=float)
        values = fitted.predict(x_matrix)
        confidence = fitted.predict_confidence(x_matrix)
        positive_probability = fitted.predict_positive_probability(x_matrix)
    out = Path(str(payload["output_dir"])) / f"{int(payload['shard_id'])}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "key": np.asarray(keys, dtype=np.int64),
        "value": np.asarray(values, dtype=float),
        "confidence": np.full(len(keys), np.nan) if confidence is None else np.asarray(confidence, dtype=float),
        "positive_probability": (np.full(len(keys), np.nan) if positive_probability is None
                                 else np.asarray(positive_probability, dtype=float)),
    }).to_parquet(out, index=False)
    return {"path": str(out), "count": len(keys)}


def _descriptor_graph_payload(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Shape descriptor rows for DESCRIPTOR_GRAPH_OUTPUT: descriptor values upserted onto
    MoleculeRecord columns, optional Morgan fingerprints upserted into FingerprintRecord."""
    molecules: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "") != "completed":
            continue
        molecule_id = int(row.get("molecule_id") or 0)
        molecules.append({"id": molecule_id, **{col: row.get(col) for col in DESCRIPTOR_COLUMNS}})
        if row.get("fp_binary"):
            fingerprints.append({
                "molecule_id": molecule_id,
                "fp_type": row["fp_type"],
                "nbits": int(row["fp_nbits"]),
                "radius": int(row["fp_radius"]),
                "fp_binary": row["fp_binary"],
            })
    return {"molecules": molecules, "fingerprints": fingerprints}
