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
    rows = calculate_descriptor_rows(list(payload.get("items") or []), fingerprint=payload.get("fp"),
                                     rdkit2d=bool(payload.get("rdkit2d")))
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


def _bits(mol, radius: int, nbits: int):
    import numpy as np

    from amdockvs.chemistry.fingerprints import morgan_fingerprint

    bits = morgan_fingerprint(mol, radius=int(radius), n_bits=int(nbits))
    return np.frombuffer(bits.ToBitString().encode("ascii"), dtype=np.uint8) - ord("0")


def _feature_vector(mol, fitted) -> list[float] | None:
    """What the model was trained on, computed from the molecule: the row path reads the same
    values off the stored fingerprint or the rdkit2d block."""
    import numpy as np

    from amdockvs.chemistry.descriptors import calculate_rdkit2d_descriptors

    if fitted.feature_kind == "ecfp4":
        return list(_bits(mol, fitted.fp_radius or 2, fitted.fp_nbits or 2048))
    values = calculate_rdkit2d_descriptors(mol)
    return [float(values.get(name, np.nan)) for name in fitted.feature_names] if values else None


def _domain(mols, fitted):
    """Each molecule's applicability-domain similarity to the model's training set, or Nones
    for a model that stored no training fingerprints."""
    from amdockvs.qsar.modeling import domain_similarity

    train = fitted.training_bits()
    if train is None or not mols:
        return [None] * len(mols)
    query = [_bits(mol, fitted.ad_fp_radius, fitted.ad_fp_nbits) for mol in mols]
    return [float(v) for v in domain_similarity(train, query)]


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
    keys, rows, mols = [], [], []
    for record_id, mol in zip(*read_shard(payload["shard_path"])):
        try:
            vector = None if mol is None else _feature_vector(mol, fitted)
        except Exception:  # noqa: BLE001 - one odd molecule does not fail its shard
            vector = None
        if vector is not None:
            keys.append(shard_key(payload.get("import_batch_id"), record_id))
            rows.append(vector)
            mols.append(mol)
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
        "ad_similarity": np.asarray([np.nan if v is None else v for v in _domain(mols, fitted)], dtype=float),
    }).to_parquet(out, index=False)
    return {"path": str(out), "count": len(keys)}


@task(
    name="amdock_qsar_train_model",
    description="Fit, validate and save one QSAR model.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def train_model_task(payload: dict) -> dict:
    """One model of a train job: the design matrix comes by file, shared by every model of the job;
    the fitted model is written beside the others, named by its identity."""
    from pathlib import Path

    import numpy as np

    from amdockvs.qsar.modeling import save_model
    from amdockvs.qsar.training import train_on_arrays

    data = np.load(str(payload["data_path"]), allow_pickle=False)
    spec = dict(payload["spec"])
    groups = data["groups"].tolist() if data["groups"].size else None
    names = data["names"].tolist() if data["names"].size else [f"bit_{i}" for i in range(data["x"].shape[1])]
    fitted, record, members = train_on_arrays(
        x=data["x"].astype(float), y=data["y"], ids=data["ids"].tolist(), is_test=data["is_test"],
        folds=data["folds"], groups=groups, names=names, spec=spec,
    )
    if "ad_fp" in data and data["ad_fp"].size:
        fitted.ad_fingerprints = data["ad_fp"]
        fitted.ad_fp_radius, fitted.ad_fp_nbits = int(data["ad_radius"]), int(data["ad_nbits"])
    out = Path(str(payload["output_dir"])) / f"model_{spec['identity'][:16]}.joblib"
    out.parent.mkdir(parents=True, exist_ok=True)
    save_model(out, fitted)
    ref = f"model::{spec['identity']}"
    return {
        "qsar_models": [{**record, "model_path": str(out), "$ref": ref}],
        "qsar_model_members": [{**member, "model_ref": ref} for member in members],
    }


@task(
    name="amdock_qsar_predict_rows",
    description="Score one batch of molecules with a QSAR model.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def predict_rows_task(payload: dict) -> dict:
    """Features computed from each molecule's structure, the same way the shards are scored; a
    molecule that does not parse, or whose features cannot be computed, is left unscored."""
    import numpy as np

    from amdockvs.chemistry.structures import mol_from_bytes
    from amdockvs.qsar.modeling import load_model

    fitted = load_model(payload["model_path"])
    ids, rows, mols = [], [], []
    for item in payload.get("items") or []:
        try:
            mol = mol_from_bytes(item["structure"], item.get("structure_kind"))
            vector = None if mol is None else _feature_vector(mol, fitted)
        except Exception:  # noqa: BLE001 - one odd molecule does not fail its batch
            vector = None
        if vector is not None:
            ids.append(int(item["molecule_id"]))
            rows.append(vector)
            mols.append(mol)
    if not rows:
        return {"qsar_predictions": []}
    x_matrix = np.asarray(rows, dtype=float)
    values = fitted.predict(x_matrix)
    confidence = fitted.predict_confidence(x_matrix)
    probability = fitted.predict_positive_probability(x_matrix)
    domain = _domain(mols, fitted)
    model_id = int(payload["model_id"])
    return {"qsar_predictions": [
        {
            "molecule_id": mid, "model_id": model_id, "value": float(values[i]),
            "confidence": None if confidence is None else float(confidence[i]),
            "positive_probability": None if probability is None else float(probability[i]),
            "ad_similarity": domain[i],
        }
        for i, mid in enumerate(ids)
    ]}


def _descriptor_graph_payload(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Shape descriptor rows for DESCRIPTOR_GRAPH_OUTPUT: descriptor values upserted onto
    MoleculeRecord columns, optional Morgan fingerprints upserted into FingerprintRecord."""
    molecules: list[dict[str, Any]] = []
    fingerprints: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
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
        if row.get("rdkit2d"):
            blocks.append({"molecule_id": molecule_id, "block": "rdkit2d", "values_json": row["rdkit2d"]})
    return {"molecules": molecules, "fingerprints": fingerprints, "descriptor_blocks": blocks}
