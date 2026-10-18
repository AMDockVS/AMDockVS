"""What a worker runs to change the chemistry of molecules: of rows, of shards, of a predicted structure.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ms_flow.payloads import scratch_file

from amdockvs.chemistry.service import transform_ligand_rows, transform_receptor_rows
from amdockvs.chemistry.shards import transform_ligand_shard
from amdockvs.chemistry.tools.esmfold import predict_structure, sequence_from_structure
from amdockvs.core.constants import AMDOCKVS_OFFLOADABLE_EXECUTORS, SERIES_AUX, SERIES_RECEPTORS
from amdockvs.core.jobs import task
from amdockvs.core.payloads import structure_payload
from amdockvs.core.rows import molecule_model_row
from amdockvs.core.vocab import ModelSource, ShardState


def _chemistry_graph_payload(
    updates: list[dict[str, Any]], failures: list[dict[str, Any]] = ()
) -> dict[str, list[dict[str, Any]]]:
    """Map chemistry updates into the molecules-upsert / molecule_models-insert graph payload.

    A molecule that failed is marked excluded with the step and error as the reason: later
    steps skip it, and Filter's "Excluded" scope lists it and brings it back.
    """
    now = datetime.now()
    molecules: list[dict[str, Any]] = []
    molecule_models: list[dict[str, Any]] = []
    for update in updates:
        entity_id = int(update.get("entity_id") or 0)
        if entity_id <= 0:
            continue
        mol_row: dict[str, Any] = {
            "id": entity_id,
            "extra_data": dict(update.get("extra_data") or {}),
            "updated_at": now,
        }
        state = dict(update.get("state") or {})
        for key in ("has_3d", "has_hs", "is_minimized", "conformer_count"):
            if key in state:
                mol_row[key] = state[key]
        if "current_payload_id" in update:
            mol_row["current_payload_id"] = update["current_payload_id"]
        if "current_model_index" in update:
            current_model_index = update.get("current_model_index")
            mol_row["current_model_index"] = None if current_model_index is None else int(current_model_index)
        molecules.append(mol_row)
        for model_row in list(update.get("model_rows") or []):
            molecule_models.append(dict(model_row))
    failed = [
        {"id": int(f["entity_id"]), "excluded": True, "exclusion_reason": str(f.get("error") or ""), "updated_at": now}
        for f in failures
        if int(f.get("entity_id") or 0) > 0
    ]
    return {"molecules": molecules, "molecule_models": molecule_models, "molecule_failures": failed}


@task(
    name="amdock_ligand_chemistry_batch",
    description="Apply a reusable ligand chemistry operation and persist metadata updates.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def ligand_chemistry_task(payload: dict, progress_cb=None):
    result = transform_ligand_rows(
        operations=payload.get("operation") or "",
        rows=list(payload.get("rows") or []),
        params=dict(payload.get("params") or {}),
        next_model_index_by_entity=dict(payload.get("next_model_index_by_entity") or {}),
        progress_cb=progress_cb,
    )
    updates = list(result.get("updates") or [])
    failure_count = int(result.get("failure_count") or 0)
    processed_count = int(result.get("processed_count") or len(updates) + failure_count)
    if processed_count > 0 and not updates and failure_count > 0:
        sample = list(result.get("failure_samples") or [])
        raise RuntimeError(
            "Ligand chemistry batch failed for every molecule. "
            f"processed={processed_count} failed={failure_count} samples={sample[:3]}"
        )
    return _chemistry_graph_payload(updates, list(result.get("failures") or []))


@task(
    name="amdock_receptor_chemistry_batch",
    description="Apply a reusable receptor chemistry operation and persist metadata updates.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def receptor_chemistry_task(payload: dict, progress_cb=None):
    rows = list(payload.get("rows") or [])
    updates = transform_receptor_rows(
        operation=str(payload.get("operation") or ""),
        rows=rows,
        params=dict(payload.get("params") or {}),
        next_model_index_by_entity=dict(payload.get("next_model_index_by_entity") or {}),
        progress_cb=progress_cb,
    )
    if list(payload.get("rows") or []) and not updates:
        raise RuntimeError("Receptor chemistry batch produced no updates.")
    return _chemistry_graph_payload(list(updates or []))


@task(
    name="amdock_receptor_prediction",
    description="Predict one protein structure from its sequence with the ESMFold API.",
    executor="thread",
    supported_executors=("thread",),
)
def receptor_prediction_task(payload: dict, progress_cb=None):
    """One protein per chunk: a failed call fails that protein only, not credits spent on others."""
    row = dict(payload["row"])
    receptor_id = int(row["id"])
    sequence = str(payload.get("sequence") or "")
    if not sequence:
        if not payload.get("structure"):
            raise ValueError(f"Receptor {receptor_id} has neither a sequence nor a structure to read one from.")
        with scratch_file(payload["structure"], f".{payload.get('structure_kind') or 'cif'}") as path:
            sequence = sequence_from_structure(path)
    model_index = int(payload["model_index"])
    metrics, cif, pae = predict_structure(sequence, model=str(payload["model"]))
    if metrics.get("potential_sequence_of_concern"):
        print(f"ESMFold flagged receptor {receptor_id} as a potential sequence of concern.")
    if progress_cb is not None:
        progress_cb(100.0)

    current = structure_payload(cif, "cif", SERIES_RECEPTORS)
    return {
        "molecules": [{
            "id": receptor_id,
            # The first prediction is also the canonical structure of a protein that had none.
            "stored_payload_id": row.get("stored_payload_id") or current,
            "current_payload_id": current,
            "current_model_index": model_index,
            "has_3d": True,
            "has_hs": False,
            "is_minimized": False,
            "conformer_count": 1,
            "updated_at": datetime.now(),
        }],
        "molecule_models": [
            molecule_model_row(
                molecule_id=receptor_id,
                model_index=model_index,
                payload_id=current,
                source=ModelSource.ESMFOLD,
                metrics=metrics,
                pae_payload_id=None if pae is None else structure_payload(pae, "npy", SERIES_AUX),
            )
        ],
    }


@task(
    name="amdock_shard_chemistry",
    description="Run the ligand chemistry pipeline over one shard file.",
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def shard_chemistry_task(payload: dict, progress_cb=None) -> list[dict]:
    shard_path = Path(str(payload["shard_path"])).expanduser().resolve()
    output_path = Path(str(payload["output_dir"])).expanduser().resolve() / shard_path.name
    stats = transform_ligand_shard(shard_path, output_path, payload["steps"])
    # A shard whose molecules all failed is still a done shard: it holds zero records now.
    return [{
        "source": payload["source"],
        "shard_index": int(payload["shard_index"]),
        "path": str(output_path),
        "input_format": payload.get("input_format", ""),
        "n_records": int(stats["n_records"]),
        # Pending, not ready: what a generation's shards have had run over them is the
        # generation's `step`, not a flag on each row. Stamping `ready` here made the next
        # chemistry step (whose feed is `state=pending`) find an empty library.
        "state": ShardState.PENDING,
        "error": "" if not stats["n_failed"] else f"{stats['n_failed']} of {stats['n_input']} molecules failed",
        "updated_at": datetime.now(),
    }]
