from __future__ import annotations

from itertools import batched
from pathlib import Path
import json
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec
from ms_flow.sinks import table_sink
from ms_flow.tasking import JobSpec

from amdockvs.configuration import batch_size_for
from amdockvs.api_common import project_root_from_output_dir, worker_output_dir, worker_path_fields
from amdockvs.constants import AMDOCKVS_LOCAL_EXECUTORS
from amdockvs.docking.repository import (
    iter_entity_rows,
    molecule_scope_spec,
    project_db_path,
    resolve_storage_dir,
)
from amdockvs.docking.service import prepare_entities_rows, prepared_path_from_row
from amdockvs.models import EngineState
from amdockvs.molecule_paths import preferred_molecule_path, set_default_project_root


ENGINE_STATE_UPSERT = table_sink(
    model=EngineState,
    write_mode="upsert",
    conflict_keys=("molecule_id", "role_type", "engine"),
)


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
            EngineState.build_row(
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
            EngineState.build_row(
                molecule_id=molecule_id,
                role_type=role_type,
                engine=str(failure.get("engine") or summary.get("engine") or "ad4"),
                files={key: value for key, value in files.items() if str(value or "").strip()},
                is_ready=False,
            )
        )
    return rows_out


class PreparationJobParams(BaseModel):
    # None -> settings (batch_sizes.ligand / batch_sizes.receptor), resolved at chunk-build time.
    batch_size: int | None = Field(default=None, ge=1)
    ligand_set_id: int | None = Field(default=None, ge=1)
    receptor_set_id: int | None = Field(default=None, ge=1)
    ligand_filters: dict[str, Any] = Field(default_factory=dict)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)
    engine: str = "ad4"
    force: bool = False
    # Receptors only: what import kept is not automatically what gets docked. Off by default,
    # so a receptor prepared today is the same dry structure it was before these existed.
    keep_waters: bool = False
    keep_cofactors: bool = False


def scope_spec(params: PreparationJobParams, *, entity_kind: str) -> QuerySpec:
    """The scope of a preparation, fully declared: the same WHERE that feeds the feed.

    `db_count` over this spec gives the ceiling of the scope. It is not the exact total: with
    `force=False` the feed additionally drops rows whose prepared file already exists **on
    disk**, and that is not a query (the DB flag lies if someone deleted the file). That is why
    this job does not declare `total_chunks` — over-declaring would leave it never completing.
    """
    return molecule_scope_spec(
        entity_kind=entity_kind,
        engine=params.engine,
        set_id=params.ligand_set_id if entity_kind == "ligand" else params.receptor_set_id,
        filters=params.ligand_filters if entity_kind == "ligand" else params.receptor_filters,
        fields=("id", "stored_path", "current_path", "has_3d"),
        order=("id",),
    )


def _rows_for_preparation(
    *,
    project_db,
    entity_kind: str,
    engine: str,
    set_id: int | None,
    filters: Mapping[str, Any] | None,
    force: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    rows = iter_entity_rows(
        project_db,
        entity_kind=entity_kind,
        engine=engine,
        set_id=set_id,
        filters=filters,
        fields=("id", "stored_path", "current_path", "has_3d", "prepared_engine_path", "prepared_files"),
        order=("id",),
        batch_size=batch_size,
    )
    if force:
        return rows

    def is_pending(row) -> bool:
        prepared = prepared_path_from_row(row, engine=engine)
        return prepared is None or not prepared.exists()

    return filter(is_pending, rows)


def _metadata_has_3d(raw_metadata: str | None) -> bool | None:
    if not raw_metadata:
        return None
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    chemistry = metadata.get("chemistry")
    if isinstance(chemistry, dict):
        current = chemistry.get("current")
        if isinstance(current, dict):
            if "has_3d" in current:
                return bool(current.get("has_3d"))
            current_state = current.get("state")
            if isinstance(current_state, dict) and "has_3d" in current_state:
                return bool(current_state.get("has_3d"))
    state = metadata.get("state")
    if isinstance(state, dict) and "has_3d" in state:
        return bool(state.get("has_3d"))
    return None


def _path_has_3d(path: str | Path) -> bool:
    source = Path(path).expanduser().resolve()
    suffix = source.suffix.lower()
    if suffix == ".pdbqt":
        return True
    if suffix in {".sdf", ".sd", ".mol"}:
        from rdkit import Chem

        supplier = Chem.SDMolSupplier(str(source), removeHs=False)
        mol = supplier[0] if supplier and len(supplier) > 0 else None
        if mol is None or mol.GetNumConformers() == 0:
            return False
        return any(bool(mol.GetConformer(index).Is3D()) for index in range(mol.GetNumConformers()))
    if suffix in {".pdb", ".mol2"}:
        return True
    return False


def _filter_ligands_with_3d(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for row in rows:
        has_3d = row.get("has_3d")
        if has_3d is None:
            stored_path = preferred_molecule_path(row)
            has_3d = bool(stored_path and _path_has_3d(stored_path))
        if has_3d:
            valid.append(row)
    return valid


def _attach_flexible_residues(project_db, rows: list[dict[str, Any]]) -> None:
    from amdockvs.models import MoleculeRecord

    with project_db.get_session() as session:
        for row in rows:
            rec = session.get(MoleculeRecord, int(row.get("id") or 0))
            if rec is None:
                continue
            keys = list((dict(rec.extra_data or {}).get("flexible_residues") or []))
            if keys:
                row["flexible_residues"] = [str(k) for k in keys]


def _iter_preparation_batches(
    *,
    project_db,
    db_path: Path,
    entity_kind: str,
    output_dir: Path,
    params: PreparationJobParams,
) -> Iterator[dict[str, Any]]:
    set_id = params.ligand_set_id if entity_kind == "ligand" else params.receptor_set_id
    filters = params.ligand_filters if entity_kind == "ligand" else params.receptor_filters
    batch_size = max(1, int(params.batch_size or batch_size_for(entity_kind)))
    rows_iter = _rows_for_preparation(
        project_db=project_db,
        entity_kind=entity_kind,
        engine=params.engine,
        set_id=set_id,
        filters=filters,
        force=bool(params.force),
        batch_size=batch_size,
    )

    def chunk(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "entity_kind": entity_kind,
            "engine": params.engine,
            "output_dir": worker_output_dir(output_dir),
            "rows": [worker_path_fields(row, "stored_path", "current_path") for row in rows],
            "keep_waters": bool(params.keep_waters),
            "keep_cofactors": bool(params.keep_cofactors),
        }

    emitted = False
    for batch in batched(rows_iter, batch_size):
        rows = list(batch)
        if entity_kind == "receptor":
            # Flexible residues live on the receptor's extra_data; carry them into the worker payload
            # (the row fields don't include extra_data). Receptors are few, so per-row lookup is fine.
            _attach_flexible_residues(project_db, rows)
        emitted = True
        yield chunk(rows)
    if not emitted:
        # MF still needs one chunk to close the job out cleanly when the scope is empty.
        yield chunk([])


def _build_preparation_chunks(
    *,
    params: dict,
    config: dict | None,
    entity_kind: str,
) -> Iterator[dict[str, Any]]:
    parsed = PreparationJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError(f"prepare_{entity_kind}s_job requires project_db in config.")
    yield from _iter_preparation_batches(
        project_db=project_db,
        db_path=project_db_path(project_db),
        entity_kind=entity_kind,
        output_dir=resolve_storage_dir(entity_kind=entity_kind, config=config_map),
        params=parsed,
    )


class _PrepareEntitiesJobSpec(JobSpec):
    description = "Prepare ligands or receptors for AutoDock Vina with Meeko and persist EngineState rows."
    params_model = PreparationJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = ENGINE_STATE_UPSERT
    output_flush_every = 1
    store_results = False

    @staticmethod
    def run_chunk(payload: dict, progress_cb=None):
        output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
        set_default_project_root(project_root_from_output_dir(output_dir))
        entity_kind = str(payload.get("entity_kind") or "")
        summary = prepare_entities_rows(
            entity_kind=entity_kind,
            engine=str(payload.get("engine") or "ad4"),
            output_dir=output_dir,
            rows=list(payload.get("rows") or []),
            keep_waters=bool(payload.get("keep_waters")),
            keep_cofactors=bool(payload.get("keep_cofactors")),
            progress_cb=progress_cb,
        )
        return _engine_state_rows_from_summary(summary, entity_kind=entity_kind)


class PrepareLigandsJobSpec(_PrepareEntitiesJobSpec):
    name = "amdock_prepare_ligands_job"
    task_name = "amdock_prepare_ligands_task"
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        yield from _build_preparation_chunks(params=params, config=config, entity_kind="ligand")


class PrepareReceptorsJobSpec(_PrepareEntitiesJobSpec):
    name = "amdock_prepare_receptors_job"
    task_name = "amdock_prepare_receptors_task"
    required = ()
    produces = ()

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict[str, Any]]:
        yield from _build_preparation_chunks(params=params, config=config, entity_kind="receptor")


prepare_ligands_job = PrepareLigandsJobSpec.to_job_definition()
prepare_receptors_job = PrepareReceptorsJobSpec.to_job_definition()


__all__ = [
    "PreparationJobParams",
    "PrepareLigandsJobSpec",
    "PrepareReceptorsJobSpec",
    "prepare_ligands_job",
    "prepare_receptors_job",
]
