"""What a worker runs to prepare docking inputs: entities of the project, or shards of a library.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from amdockvs.core.rows import prep_row
from amdockvs.docking.preparation.profiles import get_preparation_profile


def _prep_rows_from_summary(summary: dict, *, entity_kind: str) -> list[dict[str, Any]]:
    """Map a preparation summary into receptor_preps / ligand_preps upsert rows.

    Successful and failed attempts are both persisted so Docking Studio can show why a
    molecule is not ready instead of leaving the failure invisible.
    """
    role_type = str(entity_kind or "").strip().lower()
    rows_out: list[dict[str, Any]] = []
    for update in list(summary.get("updates") or []):
        molecule_id = int(update.get("entity_id") or 0)
        if molecule_id <= 0:
            continue
        rows_out.append(
            prep_row(
                role_type=role_type,
                molecule_id=molecule_id,
                family=str(update.get("engine") or "ad4"),
                payload_id=update["prepared"],
                flex_payload_id=update.get("flex"),
                binding_site_id=int(update.get("binding_site_id") or 0),
                # Stored on the row: it is the only place the Preparation table can show them.
                warnings="\n".join(update.get("warnings") or []) or None,
            )
        )
    for failure in list(summary.get("failures") or []):
        molecule_id = int(failure.get("entity_id") or 0)
        if molecule_id <= 0:
            continue
        rows_out.append(
            prep_row(
                role_type=role_type,
                molecule_id=molecule_id,
                family=str(failure.get("engine") or summary.get("engine") or "ad4"),
                binding_site_id=int(failure.get("binding_site_id") or 0),
                error=str(failure.get("error") or "Preparation failed."),
            )
        )
    return rows_out


def prepare_entities(payload: dict, progress_cb=None):
    entity_kind = str(payload.get("entity_kind") or "")
    profile = get_preparation_profile(str(payload.get("engine") or "ad4"))
    summary = profile.prepare_entities(
        entity_kind=entity_kind,
        engine=str(payload.get("engine") or "ad4"),
        rows=list(payload.get("rows") or []),
        keep_waters=bool(payload.get("keep_waters")),
        keep_cofactors=bool(payload.get("keep_cofactors")),
        ion_charges=bool(payload.get("ion_charges")),
        progress_cb=progress_cb,
    )
    preps = _prep_rows_from_summary(summary, entity_kind=entity_kind)
    # ponytail: every failure is excluded, even an environment one (missing tool); Filter's
    # "Excluded" scope brings them back. Skip whole-batch failures if that bites.
    failures = [
        {
            "id": row.get("molecule_id") or row.get("receptor_molecule_id"),
            "excluded": True,
            "exclusion_reason": f"Docking preparation ({row['family']}): {(row['error'].strip() or 'failed').splitlines()[0]}",
        }
        for row in preps
        if row["error"] is not None
    ]
    return {f"{entity_kind}_preps": preps, "molecule_failures": failures}


def prepare_ligand_shards(payload: dict, progress_cb=None):
    """Each shard into its own derived file (`catalog_sink` registers it as the chunk arrives).
    The name is new per run, so a redo never overwrites a file a catalog row still points at;
    a campaign's prep, which `located` must find there, names it (`output_name`)."""
    from uuid import uuid4

    from ms_flow.core.data.catalog import file_row

    engine = str(payload.get("engine") or "ad4")
    profile = get_preparation_profile(engine)
    if profile.prepare_shard is None:
        raise ValueError(f"Preparation profile '{engine}' does not support sharded libraries.")
    rows: list[dict[str, Any]] = []
    for index, shard in enumerate(list(payload.get("shards") or [])):
        source = Path(str(shard["shard_path"])).expanduser().resolve()
        name = shard.get("output_name") or f"{int(shard['shard_id'])}-{uuid4().hex[:8]}{source.suffix}"
        output_path = Path(str(shard["output_dir"])).expanduser().resolve() / name
        profile.prepare_shard(source, output_path, engine=engine, ids=shard.get("ids"),
                              scope=shard.get("import_batch_id"), origin=int(shard["shard_id"]))
        if output_path.is_file():
            rows.append(file_row(output_path, role=str(payload["role"]),
                                 generation_id=shard.get("generation_id")))
        if progress_cb is not None:
            progress_cb(((index + 1) / max(1, len(payload["shards"]))) * 100.0)
    return rows