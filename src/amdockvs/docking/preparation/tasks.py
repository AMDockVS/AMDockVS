"""What a worker runs to prepare docking inputs: entities of the project, or shards of a library.

No SQL and no job declaration here: a worker loads this module and nothing of `jobs`, `api` or `models`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from amdockvs.core.paths import set_default_project_root
from amdockvs.core.rows import engine_state_row, shard_engine_state_row
from amdockvs.core.worker_io import project_root_from_output_dir
from amdockvs.docking.preparation.profiles import get_preparation_profile


def _engine_state_rows_from_summary(summary: dict, *, entity_kind: str) -> list[dict[str, Any]]:
    """Map a preparation summary into EngineState upsert rows.

    Successful and failed attempts are both persisted so Docking Studio can show why a
    molecule is not ready instead of leaving the failure invisible.
    """
    role_type = str(entity_kind or "").strip().lower()
    rows_out: list[dict[str, Any]] = []
    for update in list(summary.get("updates") or []):
        molecule_id = int(update.get("entity_id") or 0)
        if molecule_id <= 0:
            continue
        prepared_path = str(update.get("prepared_path") or "").strip()
        files = {"prepared": str(Path(prepared_path).expanduser().resolve())} if prepared_path else {}
        files.update(dict(update.get("files") or {}))
        source_path = str(update.get("source_path") or "").strip()
        if source_path:
            files["source"] = source_path
        rows_out.append(
            engine_state_row(
                molecule_id=molecule_id,
                role_type=role_type,
                engine=str(update.get("engine") or "ad4"),
                files=files,
                is_ready=bool(prepared_path),
            )
        )
    for failure in list(summary.get("failures") or []):
        molecule_id = int(failure.get("entity_id") or 0)
        if molecule_id <= 0:
            continue
        files = {
            "error": str(failure.get("error") or "Preparation failed."),
            "source": str(failure.get("source_path") or ""),
        }
        rows_out.append(
            engine_state_row(
                molecule_id=molecule_id,
                role_type=role_type,
                engine=str(failure.get("engine") or summary.get("engine") or "ad4"),
                files={key: value for key, value in files.items() if str(value or "").strip()},
                is_ready=False,
            )
        )
    return rows_out


def prepare_entities(payload: dict, progress_cb=None):
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    set_default_project_root(project_root_from_output_dir(output_dir))
    entity_kind = str(payload.get("entity_kind") or "")
    profile = get_preparation_profile(str(payload.get("engine") or "ad4"))
    summary = profile.prepare_entities(
        entity_kind=entity_kind,
        engine=str(payload.get("engine") or "ad4"),
        output_dir=output_dir,
        rows=list(payload.get("rows") or []),
        keep_waters=bool(payload.get("keep_waters")),
        keep_cofactors=bool(payload.get("keep_cofactors")),
        progress_cb=progress_cb,
    )
    return _engine_state_rows_from_summary(summary, entity_kind=entity_kind)


def prepare_ligand_shards(payload: dict, progress_cb=None):
    engine = str(payload.get("engine") or "ad4")
    rows: list[dict[str, Any]] = []
    for shard in list(payload.get("shards") or []):
        source = Path(str(shard["shard_path"])).expanduser().resolve()
        output_path = Path(str(shard["output_dir"])).expanduser().resolve() / source.name
        profile = get_preparation_profile(engine)
        if profile.prepare_shard is None:
            raise ValueError(f"Preparation profile '{engine}' does not support sharded libraries.")
        stats = profile.prepare_shard(source, output_path, engine=engine)
        rows.append(
            shard_engine_state_row(
                shard_id=int(shard["shard_id"]),
                engine=engine,
                files={"source": str(source), "prepared": str(output_path)},
                n_records=int(stats["n_records"]),
                is_ready=output_path.is_file(),
                error=(
                    "" if not stats["n_failed"]
                    else f"{stats['n_failed']} of {stats['n_input']} ligands failed"
                ),
            )
        )
        if progress_cb is not None:
            progress_cb((len(rows) / max(1, len(payload["shards"]))) * 100.0)
    return rows
