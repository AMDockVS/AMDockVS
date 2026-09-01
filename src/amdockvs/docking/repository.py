from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from sqlite3 import OperationalError
from typing import Any, Iterable, Iterator, Mapping

from ms_flow.core.database import ProjectStore
from ms_flow.query import QuerySpec, db_count, db_pages, db_rows
from sqlmodel import select

from amdockvs.constants import (
    RESOURCE_DOCKING_RESULTS,
    RESOURCE_MOLECULES,
    TABLE_COMPLEXES,
    TABLE_DOCKING_RESULTS,
    TABLE_ENGINES,
    TABLE_MOLECULE_SET_MEMBERS,
    TABLE_MOLECULES,
    TABLE_INTERACTION_RESULTS,
)
from amdockvs.models import BindingSite, ComplexRecord, DockingResultRecord, EngineState, MoleculeRecord
from amdockvs.models.molecules import sanitize_molecule_extra_data
from amdockvs.scopes import (
    list_complex_set_ids,
    list_molecule_set_ids,
    molecule_set_spec,
    prepared_molecules_spec,
)
from amdockvs.summaries import DockingHitSummary, DockingResultsStatsSummary, ReceptorDockingSummary


def resolve_storage_dir(*, entity_kind: str, config: Mapping[str, Any] | None) -> Path:
    config_map = dict(config or {})
    resources = dict(config_map.get("project_resources") or {})
    resource = dict(resources.get(RESOURCE_MOLECULES) or {})
    path_text = str(resource.get("path") or "").strip()
    if not path_text:
        raise ValueError("Missing project resource 'molecules' for docking.")
    path = Path(path_text).expanduser().resolve() / "docking" / str(entity_kind).strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_docking_output_dir(params: Mapping[str, Any], config: Mapping[str, Any] | None) -> Path:
    explicit = str(params.get("output_dir") or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    resources = dict((config or {}).get("project_resources") or {})
    docking_resource = dict(resources.get(RESOURCE_DOCKING_RESULTS) or {})
    resource_path = str(docking_resource.get("path") or "").strip()
    if resource_path:
        path = Path(resource_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    raise ValueError("output_dir is required when project resource 'docking_results' is not available.")


def project_db_path(project_db) -> Path:
    db_path = getattr(project_db, "db_path", None)
    if db_path is None:
        raise ValueError("Docking repository requires project_db.db_path.")
    return Path(db_path).expanduser().resolve()


def _project_root(project_db) -> Path:
    return project_db_path(project_db).parent


def _binding_site_payload(site: BindingSite | None) -> dict[str, Any] | None:
    if site is None or not site.is_defined:
        return None
    extra = sanitize_molecule_extra_data(site.extra_data)
    return {
        "engine": "vina",
        "binding_site_id": int(site.id or 0) or None,
        "center": [float(site.center_x or 0.0), float(site.center_y or 0.0), float(site.center_z or 0.0)],
        "size": [float(site.size_x or 0.0), float(site.size_y or 0.0), float(site.size_z or 0.0)],
        "spacing": float(extra.get("spacing") or 0.375),
    }


_HYDRATE_ID_CHUNK = 500  # SQLite: max ~999 bound parameters per statement


def _augment_molecule_rows(
    project_db,
    rows: list[dict[str, Any]],
    *,
    role_type: str,
    engine: str = "ad4",
) -> list[dict[str, Any]]:
    """Attach engine/grid state to a batch of rows with two queries, not two per row."""
    wanted = [row for row in rows if int(row.get("id") or 0) > 0]
    if not wanted:
        return rows
    ids = sorted({int(row["id"]) for row in wanted})
    # Active sites only: it used to fetch every site of each molecule just to discard them.
    site_ids = sorted({int(row.get("active_binding_site_id") or 0) for row in wanted} - {0})
    states: dict[int, EngineState] = {}
    sites: dict[int, BindingSite] = {}
    with project_db.get_session() as session:
        for offset in range(0, len(ids), _HYDRATE_ID_CHUNK):
            chunk = ids[offset:offset + _HYDRATE_ID_CHUNK]
            for state in session.exec(
                select(EngineState)
                .where(EngineState.molecule_id.in_(chunk))
                .where(EngineState.role_type == str(role_type))
                .where(EngineState.engine == str(engine))
            ).all():
                states[int(state.molecule_id)] = state
        for offset in range(0, len(site_ids), _HYDRATE_ID_CHUNK):
            chunk = site_ids[offset:offset + _HYDRATE_ID_CHUNK]
            for site in session.exec(
                select(BindingSite).where(BindingSite.id.in_(chunk))
            ).all():
                sites[int(site.id or 0)] = site

    prepared_flag_key = f"prepared_{str(engine).strip().lower()}"
    prepared_path_key = f"{prepared_flag_key}_path"
    grid_flag_key = f"grid_{str(engine).strip().lower()}"
    grid_payload_key = f"{grid_flag_key}_payload"
    for row in rows:
        molecule_id = int(row.get("id") or 0)
        if molecule_id <= 0:
            continue
        engine_state = states.get(molecule_id)
        active_site = sites.get(int(row.get("active_binding_site_id") or 0))
        files = dict(getattr(engine_state, "files", {}) or {}) if engine_state is not None else {}
        prepared_path = str(files.get("prepared") or "").strip()
        payload = _binding_site_payload(active_site)
        prepared_ready = bool(engine_state is not None and bool(engine_state.is_ready))
        row["prepared_engine"] = prepared_ready
        row["prepared_engine_path"] = prepared_path
        row[prepared_flag_key] = prepared_ready
        row[prepared_path_key] = prepared_path
        row["prepared_files"] = files
        grid_ready = bool(payload is not None)
        row["grid_engine"] = grid_ready
        row["grid_engine_payload"] = payload
        row[grid_flag_key] = grid_ready
        row[grid_payload_key] = payload
        row["metadata_json"] = json.dumps(sanitize_molecule_extra_data(row.get("extra_data")), ensure_ascii=True)
    return rows


def _augment_molecule_row(
    project_db,
    row: dict[str, Any],
    *,
    role_type: str,
    engine: str = "ad4",
) -> dict[str, Any]:
    return _augment_molecule_rows(project_db, [row], role_type=role_type, engine=engine)[0]


def _entity_scope_filters(
    entity_kind: str,
    filters: Mapping[str, Any] | None,
    *,
    engine: str,
) -> tuple[str, dict[str, Any], Any, Any]:
    """Split a scope into (kind, real column filters, prepared flag, grid flag).

    Everything left in the filter dict is compiled straight to SQL, so the virtual keys have
    to come out here: `prepared` / `prepared_engine_key` (how the molecules API spells it),
    `prepared_engine` / `grid_engine` and their per-engine variants. Leaking one of them
    produces "no such column: prepared".
    """
    normalized_kind = str(entity_kind).strip().lower()
    if normalized_kind not in {"ligand", "receptor"}:
        raise ValueError(f"Unsupported entity_kind: {entity_kind}")
    scope_filters = dict(filters or {})
    engine_key = str(engine).strip().lower()
    # The engine is fixed by the caller here, so a scope-level engine key is redundant; drop it
    # rather than second-guess a caller that asked about a different family.
    scope_filters.pop("prepared_engine_key", None)
    prepared_flag = next(
        (
            value
            for value in (
                scope_filters.pop(f"prepared_{engine_key}", None),
                scope_filters.pop("prepared_engine", None),
                scope_filters.pop("prepared", None),
            )
            if value is not None
        ),
        None,
    )
    grid_flag = next(
        (
            value
            for value in (
                scope_filters.pop(f"grid_{engine_key}", None),
                scope_filters.pop("grid_engine", None),
            )
            if value is not None
        ),
        None,
    )
    scope_filters.setdefault("excluded", False)
    scope_filters.setdefault("is_ligand" if normalized_kind == "ligand" else "is_receptor", True)
    return normalized_kind, scope_filters, prepared_flag, grid_flag


def docked_ligands_spec(
    *,
    receptor_id: int,
    engine: str,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
) -> QuerySpec:
    """Ligands already docked against this receptor — the "already computed" guard."""
    filters: dict[str, Any] = {
        "receptor_molecule_id": int(receptor_id),
        "engine": str(engine).strip().lower(),
    }
    kind = str(run_kind or "").strip()
    if kind:
        filters["metrics->run_kind"] = kind
    proto = str(protocol_hash or "").strip()
    if proto:
        filters["metrics->protocol->hash"] = proto
    return QuerySpec(table=TABLE_DOCKING_RESULTS, fields=("ligand_molecule_id",), filters=filters)


def molecule_scope_spec(
    *,
    entity_kind: str,
    engine: str,
    set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    extra_filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = ("id",),
    order: tuple[str, ...] = ("id",),
    limit: int | None = None,
    ignore_grid_filter: bool = False,
) -> QuerySpec:
    """The scope of list_entity_rows as a single QuerySpec, resolved entirely in SQL."""
    kind, scope_filters, prepared_flag, grid_flag = _entity_scope_filters(entity_kind, filters, engine=engine)
    if grid_flag is not None and not ignore_grid_filter:
        # A grid lives in binding_sites and is only resolved by the hydrating path.
        raise ValueError("molecule_scope_spec cannot filter by grid state — use list_entity_rows.")
    scope_filters.pop("_limit", None)
    if set_id is not None:
        scope_filters["id__in_subquery"] = molecule_set_spec(int(set_id))
    if prepared_flag is not None:
        key = "id__in_subquery" if bool(prepared_flag) else "id__not_in_subquery"
        spec = prepared_molecules_spec(role_type=kind, engine=engine)
        existing = scope_filters.get(key)
        scope_filters[key] = [existing, spec] if existing is not None else spec
    for raw_key, value in (extra_filters or {}).items():
        existing = scope_filters.get(raw_key)
        scope_filters[raw_key] = (
            [*(existing if isinstance(existing, list) else [existing]), value]
            if existing is not None and raw_key.endswith("_subquery")
            else value
        )
    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=fields,
        filters=scope_filters,
        order=order,
        limit=limit,
    )


def entity_ids(
    project_db,
    *,
    entity_kind: str,
    engine: str = "ad4",
    set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    limit: int | None = None,
) -> list[int]:
    """Ids matching the same scope as list_entity_rows, WITHOUT hydrating each row.

    list_entity_rows opens a session and runs two queries per row to attach engine/grid
    state; for "how many?" / "which are missing?" over a million-ligand library that is a
    million round trips. Here it is one SELECT of one column.
    """
    return list(
        _iter_entity_ids(
            project_db, entity_kind=entity_kind, engine=engine, set_id=set_id, filters=filters, limit=limit
        )
    )


def _iter_entity_ids(
    project_db,
    *,
    entity_kind: str,
    engine: str,
    set_id: int | None,
    filters: Mapping[str, Any] | None,
    limit: int | None = None,
) -> Iterable[int]:
    spec = molecule_scope_spec(
        entity_kind=entity_kind,
        engine=engine,
        set_id=set_id,
        filters=filters,
        limit=limit,
    )
    for row in db_pages(project_db, spec, page_size=1000):
        value = int(row.get("id") or 0)
        if value > 0:
            yield value


def count_entity_rows(
    project_db,
    *,
    entity_kind: str,
    engine: str = "ad4",
    set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
) -> int:
    """How many rows match the scope — one COUNT(*), nothing materialized."""
    return db_count(
        project_db,
        molecule_scope_spec(
            entity_kind=entity_kind,
            engine=engine,
            set_id=set_id,
            filters=filters,
            order=(),
        ),
    )


def iter_entity_rows(
    project_db,
    *,
    entity_kind: str,
    engine: str = "ad4",
    set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    extra_filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = ("id", "stored_path", "current_path", "input_format", "metadata_json"),
    order: tuple[str, ...] = ("id",),
    batch_size: int = 1000,
) -> Iterator[dict[str, Any]]:
    """Same scope as list_entity_rows, one row at a time.

    Set membership and preparation state are resolved by the database (subqueries in the
    scope spec). Only the grid filter stays post-hoc: it depends on the binding site
    hydration. Rows are hydrated per batch, so it is two queries per batch, not per row.
    """
    engine_key = str(engine).strip().lower()
    normalized_kind, _, _, grid_filter = _entity_scope_filters(entity_kind, filters, engine=engine_key)
    limit = dict(filters or {}).get("_limit")
    requested_fields = tuple(fields)
    model_fields = set(MoleculeRecord.model_fields)
    molecule_fields = tuple(
        dict.fromkeys(
            (
                "id",
                "stored_path",
                "current_path",
                "extra_data",
                "active_binding_site_id",
                *[field for field in requested_fields if field in model_fields],
            )
        )
    )
    spec = molecule_scope_spec(
        entity_kind=entity_kind,
        engine=engine_key,
        set_id=set_id,
        filters=filters,
        extra_filters=extra_filters,
        fields=molecule_fields,
        order=order,
        limit=None if limit is None else int(limit),
        ignore_grid_filter=True,
    )
    normalized_batch = max(1, int(batch_size))
    stream = db_pages(project_db, spec, page_size=normalized_batch)
    batch: list[dict[str, Any]] = []
    for row in stream:
        batch.append(dict(row))
        if len(batch) < normalized_batch:
            continue
        yield from _emit_entity_rows(
            project_db, batch, role_type=normalized_kind, engine=engine_key,
            grid_filter=grid_filter, requested_fields=requested_fields,
        )
        batch = []
    if batch:
        yield from _emit_entity_rows(
            project_db, batch, role_type=normalized_kind, engine=engine_key,
            grid_filter=grid_filter, requested_fields=requested_fields,
        )


def _emit_entity_rows(
    project_db,
    batch: list[dict[str, Any]],
    *,
    role_type: str,
    engine: str,
    grid_filter: Any,
    requested_fields: tuple[str, ...],
) -> Iterator[dict[str, Any]]:
    for hydrated in _augment_molecule_rows(project_db, batch, role_type=role_type, engine=engine):
        if grid_filter is not None and bool(hydrated.get("grid_engine")) is not bool(grid_filter):
            continue
        yield {field: hydrated.get(field) for field in requested_fields}


def list_entity_rows(
    project_db,
    *,
    entity_kind: str,
    engine: str = "ad4",
    set_id: int | None = None,
    filters: Mapping[str, Any] | None = None,
    fields: tuple[str, ...] = ("id", "stored_path", "current_path", "input_format", "metadata_json"),
    order: tuple[str, ...] = ("id",),
) -> list[dict[str, Any]]:
    return list(
        iter_entity_rows(
            project_db,
            entity_kind=entity_kind,
            engine=engine,
            set_id=set_id,
            filters=filters,
            fields=fields,
            order=order,
        )
    )


def list_complex_rows(
    project_db,
    *,
    set_id: int | None = None,
    purpose: str | None = None,
) -> list[dict[str, Any]]:
    rows = db_rows(
        project_db,
        TABLE_COMPLEXES,
        order=("id",),
    )
    if purpose is not None:
        allowed = {
            value.strip()
            for value in str(purpose or "").split(",")
            if value.strip()
        }
        if allowed:
            rows = [row for row in rows if str(row.get("purpose") or "") in allowed]
    if set_id is None:
        return rows
    allowed_ids = set(list_complex_set_ids(project_db, int(set_id)))
    return [row for row in rows if int(row.get("id") or 0) in allowed_ids]


def get_molecule_rows_by_ids(project_db, molecule_ids: list[int], *, engine: str = "ad4") -> dict[int, dict[str, Any]]:
    if not molecule_ids:
        return {}
    normalized_ids = sorted({int(value) for value in molecule_ids if int(value) > 0})
    resolved: dict[int, dict[str, Any]] = {}
    with project_db.get_session() as session:
        molecules = {
            int(row.id or 0): row
            for row in session.exec(
                select(MoleculeRecord).where(MoleculeRecord.id.in_(normalized_ids))
            ).all()
            if int(row.id or 0) > 0
        }
    for molecule_id, molecule in molecules.items():
        payload = molecule.model_dump(mode="python")
        role_type = "ligand" if bool(getattr(molecule, "is_ligand", False)) else "receptor" if bool(getattr(molecule, "is_receptor", False)) else ""
        if role_type:
            payload = _augment_molecule_row(project_db, payload, role_type=role_type, engine=engine)
        resolved[molecule_id] = payload
    return resolved


def get_receptor_metadata_json(project_db, *, receptor_id: int) -> str | None:
    with project_db.get_session() as session:
        receptor = session.get(MoleculeRecord, int(receptor_id))
        if receptor is None or not bool(getattr(receptor, "is_receptor", False)):
            return None
        return json.dumps(sanitize_molecule_extra_data(receptor.extra_data), ensure_ascii=True)


def update_receptor_metadata_json(project_db, *, receptor_id: int, metadata_json: str) -> None:
    with project_db.get_session() as session:
        receptor = session.get(MoleculeRecord, int(receptor_id))
        if receptor is None or not bool(getattr(receptor, "is_receptor", False)):
            raise ValueError(f"Receptor {receptor_id} does not exist in the active project.")
        try:
            receptor.extra_data = json.loads(str(metadata_json or "{}"))
        except json.JSONDecodeError:
            receptor.extra_data = {}
        receptor.updated_at = datetime.now()
        session.add(receptor)
        session.commit()


def persist_receptor_grid(
    project_db,
    *,
    receptor_id: int,
    metadata_json: str,
    grid_payload: Mapping[str, Any],
) -> None:
    del metadata_json
    center = tuple(float(value) for value in (grid_payload.get("center") or ()))
    size = tuple(float(value) for value in (grid_payload.get("size") or ()))
    if len(center) != 3 or len(size) != 3:
        raise ValueError("Grid payload requires center and size with three coordinates each.")
    with project_db.get_session() as session:
        receptor = session.get(MoleculeRecord, int(receptor_id))
        if receptor is None or not bool(getattr(receptor, "is_receptor", False)):
            raise ValueError(f"Receptor {receptor_id} does not exist in the active project.")
        site_id = int(receptor.active_binding_site_id or 0)
        site = session.get(BindingSite, site_id) if site_id > 0 else None
        if site is None:
            site = BindingSite(
                molecule_id=int(receptor_id),
                name="Manual Site",
                source="manual",
            )
        site.center_x = float(center[0])
        site.center_y = float(center[1])
        site.center_z = float(center[2])
        site.size_x = float(size[0])
        site.size_y = float(size[1])
        site.size_z = float(size[2])
        site.extra_data = {
            "engine": str(grid_payload.get("engine") or "vina"),
            "spacing": float(grid_payload.get("spacing") or 0.375),
        }
        session.add(site)
        session.flush()
        receptor.active_binding_site_id = int(site.id or 0) or None
        receptor.updated_at = datetime.now()
        session.add(receptor)
        session.commit()


def list_receptor_ids_in_set(project_db, *, receptor_set_id: int) -> list[int]:
    return list_molecule_set_ids(project_db, int(receptor_set_id))


def persist_prepared_updates(db_path: Path | str, *, entity_kind: str, updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    project_db = ProjectStore()
    project_db.connect(Path(db_path).expanduser().resolve().parent)
    try:
        now = datetime.now()
        normalized_role = str(entity_kind).strip().lower()
        with project_db.get_session() as session:
            for update in updates:
                molecule_id = int(update.get("entity_id") or 0)
                if molecule_id <= 0:
                    continue
                record = session.exec(
                    select(EngineState)
                    .where(EngineState.molecule_id == molecule_id)
                    .where(EngineState.role_type == normalized_role)
                    .where(EngineState.engine == str(update.get("engine") or "ad4"))
                ).first()
                if record is None:
                    record = EngineState(
                        molecule_id=molecule_id,
                        role_type=normalized_role,
                        engine=str(update.get("engine") or "ad4"),
                    )
                prepared_path = str(update.get("prepared_path") or "").strip()
                extra_files = dict(update.get("files") or {})
                files = {"prepared": str(Path(prepared_path).expanduser().resolve())} if prepared_path else {}
                files.update(extra_files)
                record.files = files
                record.is_ready = bool(prepared_path)
                record.updated_at = now
                if getattr(record, "created_at", None) is None:
                    record.created_at = now
                session.add(record)
            session.commit()
    finally:
        project_db.disconnect()


# --- "already computed" guard ------------------------------------------------
# A single indexed DISTINCT scan answers "which (receptor, ligand) pairs already have
# results for this engine?". Cheap even on big result tables (idx_dr_rank covers it).
# This is the docking instance of the general pattern: a job's batch builder filters its
# inputs against the outputs that already exist, so re-running only computes what's missing.

def existing_result_pairs(
    project_db,
    *,
    engine: str,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
) -> set[tuple[int, int]]:
    eng = str(engine).strip().lower()
    kind = str(run_kind or "").strip()
    proto = str(protocol_hash or "").strip()
    pairs: set[tuple[int, int]] = set()
    with suppress(OperationalError):  # results table not created until the first docking run
        with project_db.get_session() as session:
            statement = (
                select(
                    DockingResultRecord.receptor_molecule_id,
                    DockingResultRecord.ligand_molecule_id,
                )
                .where(DockingResultRecord.engine == eng)
            )
            if kind:
                statement = statement.where(
                    DockingResultRecord.metrics["run_kind"].as_string() == kind
                )
            if proto:
                statement = statement.where(
                    DockingResultRecord.metrics["protocol"]["hash"].as_string() == proto
                )
            for receptor_id, ligand_id in session.exec(statement.distinct()).all():
                pairs.add((int(receptor_id), int(ligand_id)))
    return pairs


def list_docking_result_rows(
    project_db,
    *,
    result_ids: Iterable[int] = (),
    run_id: str = "",
    receptor_id: int | None = None,
    score_lte: float | None = None,
    pose_rank: int | None = None,
) -> list[dict[str, Any]]:
    """Poses with a file and a score, for the interaction and diagram jobs.

    It goes through the ORM and not through QuerySpec because the consumer reads `metrics` as a
    dict: the model already decodes it, and a db_rows would force re-parsing the JSON by hand.
    """
    clauses = [DockingResultRecord.score.is_not(None), DockingResultRecord.pose_path != ""]
    ids = [int(value) for value in result_ids if int(value) > 0]
    if ids:
        clauses.append(DockingResultRecord.id.in_(ids))
    if str(run_id).strip():
        clauses.append(DockingResultRecord.metrics["run_id"].as_string() == str(run_id).strip())
    if receptor_id is not None:
        clauses.append(DockingResultRecord.receptor_molecule_id == int(receptor_id))
    if score_lte is not None:
        clauses.append(DockingResultRecord.score <= float(score_lte))
    if pose_rank is not None:
        clauses.append(DockingResultRecord.pose_rank == int(pose_rank))
    with project_db.get_session() as session:
        rows = session.exec(
            select(DockingResultRecord).where(*clauses).order_by(DockingResultRecord.id)
        ).all()
        return [row.model_dump(mode="python") for row in rows]


def delete_results_for_receptors(
    project_db,
    *,
    engine: str,
    receptor_ids: Iterable[int],
    protocol_hash: str | None = None,
) -> int:
    """Force-rerun helper: drop every prior result row for these receptors+engine so a
    re-dock replaces instead of duplicating. ponytail: clears ALL ligands of those receptors,
    matching 're-dock these receptors fresh'; switch to per-pair deletes if subset re-runs matter."""
    eng = str(engine).strip().lower()
    proto = str(protocol_hash or "").strip()
    ids = {int(rid) for rid in receptor_ids if int(rid) > 0}
    if not ids:
        return 0
    deleted = 0
    with suppress(OperationalError):
        with project_db.get_session() as session:
            statement = (
                select(DockingResultRecord)
                .where(DockingResultRecord.engine == eng)
                .where(DockingResultRecord.receptor_molecule_id.in_(ids))
            )
            if proto:
                statement = statement.where(
                    DockingResultRecord.metrics["protocol"]["hash"].as_string() == proto
                )
            rows = session.exec(statement).all()
            for row in rows:
                session.delete(row)
                deleted += 1
            session.commit()
    return deleted


# --- Docking results ---------------------------------------------------------------
# They used to live in a global `queries.py`: touching docking forced touching a module shared
# with molecules. They carry JOIN and aggregation, which QuerySpec does not express, so they use
# the raw `query=` escape hatch of db_rows.

def _decode_metadata(raw_metadata: str | None) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if not raw_metadata:
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {"raw_metadata_json": raw_metadata}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _coerce_project_path(project_db, raw: str | Path | None) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path.resolve() if path.is_absolute() else (_project_root(project_db) / path).resolve()


def _hit_select_sql(*, score_expr: str = "r.score", extra: str = "") -> str:
    """The SELECT/FROM shared by every hit query. `score_expr` is `MIN(r.score)` when the query
    groups by ligand: SQLite then picks the rest of the bare columns from that same (best) row."""
    return (
        "SELECT "
        f"{extra}"
        "r.id AS result_id, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.complex_id'), c.id, NULL) AS complex_id, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.run_kind'), 'screening') AS run_kind, "
        "r.ligand_molecule_id AS ligand_id, "
        "COALESCE(l.name, '') AS ligand_name, "
        "r.receptor_molecule_id AS receptor_id, "
        "COALESCE(rec.name, '') AS receptor_name, "
        "r.engine AS engine, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.protocol.label'), '') AS protocol_label, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.protocol.hash'), '') AS protocol_hash, "
        f"{score_expr} AS score, "
        "r.rmsd_vs_reference AS rmsd_vs_reference, "
        "JSON_EXTRACT(r.metrics, '$.ligand_efficiency') AS ligand_efficiency, "
        "JSON_EXTRACT(r.metrics, '$.predicted_ki_m') AS predicted_ki_m, "
        "JSON_EXTRACT(r.metrics, '$.predicted_pki') AS predicted_pki, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.lipophilic_efficiency'), JSON_EXTRACT(r.metrics, '$.lle')) AS lipophilic_efficiency, "
        "JSON_EXTRACT(r.metrics, '$.fit_quality') AS fit_quality, "
        "JSON_EXTRACT(r.metrics, '$.bei') AS bei, "
        "JSON_EXTRACT(r.metrics, '$.sei') AS sei, "
        "CASE WHEN r.score IS NULL THEN 'failed' ELSE 'completed' END AS status, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.error'), '') AS error, "
        "r.pose_path AS output_path, "
        "l.stored_path AS ligand_path, "
        "rec.current_path AS receptor_path, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.reference_ligand_path'), l.stored_path) AS reference_ligand_path, "
        "COALESCE(JSON_EXTRACT(r.metrics, '$.reference_receptor_path'), c.reference_receptor_path, rec.current_path) AS reference_receptor_path, "
        "r.metrics AS metadata_json, "
        "r.created_at AS created_at, "
        "r.created_at AS updated_at "
        f"FROM {TABLE_DOCKING_RESULTS} r "
        f"LEFT JOIN {TABLE_MOLECULES} l ON l.id = r.ligand_molecule_id "
        f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
        f"LEFT JOIN {TABLE_COMPLEXES} c ON c.receptor_molecule_id = r.receptor_molecule_id "
        f"AND c.ligand_molecule_id = r.ligand_molecule_id "
    )


def _hit_filter_clauses(
    *,
    receptor_id: int | None = None,
    ligand_id: int | None = None,
    only_completed: bool = False,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
) -> list[str]:
    clauses: list[str] = []
    if only_completed:
        clauses.append("r.score IS NOT NULL")
    if receptor_id is not None:
        clauses.append(f"r.receptor_molecule_id = {int(receptor_id)}")
    if ligand_id is not None:
        clauses.append(f"r.ligand_molecule_id = {int(ligand_id)}")
    if run_kind is not None:
        normalized_run_kind = str(run_kind or "").strip().replace("'", "''")
        if normalized_run_kind:
            clauses.append(f"COALESCE(JSON_EXTRACT(r.metrics, '$.run_kind'), 'screening') = '{normalized_run_kind}'")
    if exclude_run_kind is not None:
        excluded_run_kind = str(exclude_run_kind or "").strip().replace("'", "''")
        if excluded_run_kind:
            clauses.append(f"COALESCE(JSON_EXTRACT(r.metrics, '$.run_kind'), 'screening') != '{excluded_run_kind}'")
    if protocol_hash is not None:
        normalized_hash = str(protocol_hash).replace("'", "''")
        clauses.append(f"COALESCE(JSON_EXTRACT(r.metrics, '$.protocol.hash'), '') = '{normalized_hash}'")
    if score_lte is not None:
        clauses.append(f"r.score <= {float(score_lte)}")
    metric_expr = _metric_expr(metric_key)
    if metric_expr is not None:
        if metric_lte is not None:
            clauses.append(f"{metric_expr} <= {float(metric_lte)}")
        if metric_gte is not None:
            clauses.append(f"{metric_expr} >= {float(metric_gte)}")
    # Additive conditions from the results view's filter chips: they AND together, and two of
    # them on the same field ("gte" + "lte") are how a range gets expressed.
    for field, op, value in list(metric_filters or []):
        expr = _filter_expr(field)
        sql_op = _FILTER_OPS.get(str(op))
        if expr is not None and sql_op is not None:
            clauses.append(f"{expr} {sql_op} {float(value)}")
    return clauses


_RESULT_METRIC_KEYS = {
    "ligand_efficiency",
    "predicted_ki_m",
    "predicted_pki",
    "lipophilic_efficiency",
    "lle",
    "fit_quality",
    "bei",
    "sei",
}


_FILTER_OPS = {"gte": ">=", "lte": "<="}


def _filter_expr(field: str | None) -> str | None:
    """SQL for a filterable result field: the score column, or one of the JSON metrics."""
    key = str(field or "").strip()
    return "r.score" if key == "score" else _metric_expr(key)


def _metric_expr(metric_key: str | None) -> str | None:
    key = str(metric_key or "").strip()
    if key not in _RESULT_METRIC_KEYS:
        return None
    if key == "lipophilic_efficiency":
        return "CAST(COALESCE(JSON_EXTRACT(r.metrics, '$.lipophilic_efficiency'), JSON_EXTRACT(r.metrics, '$.lle')) AS REAL)"
    return f"CAST(JSON_EXTRACT(r.metrics, '$.{key}') AS REAL)"


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_docking_results_stats(project_db) -> DockingResultsStatsSummary:
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            # A "result" is a docked receptor-ligand PAIR, not a pose row (N poses per pair).
            # A failed pair is stored as a single row with score IS NULL (see run_vina_docking_rows).
            "COUNT(DISTINCT receptor_molecule_id || '-' || ligand_molecule_id) AS total_results, "
            "COUNT(DISTINCT CASE WHEN score IS NOT NULL THEN receptor_molecule_id || '-' || ligand_molecule_id END) AS completed_results, "
            "(COUNT(DISTINCT receptor_molecule_id || '-' || ligand_molecule_id) "
            "- COUNT(DISTINCT CASE WHEN score IS NOT NULL THEN receptor_molecule_id || '-' || ligand_molecule_id END)) AS failed_results, "
            "0 AS pending_results, "
            "COUNT(DISTINCT ligand_molecule_id) AS unique_ligands, "
            "COUNT(DISTINCT receptor_molecule_id) AS unique_receptors, "
            "MIN(score) AS best_score, "
            "AVG(score) AS avg_score, "
            "MAX(score) AS worst_score "
            f"FROM {TABLE_DOCKING_RESULTS}"
        ),
    )
    row = rows[0] if rows else {}
    return DockingResultsStatsSummary(
        total_results=int(row.get("total_results") or 0),
        completed_results=int(row.get("completed_results") or 0),
        failed_results=int(row.get("failed_results") or 0),
        pending_results=int(row.get("pending_results") or 0),
        unique_ligands=int(row.get("unique_ligands") or 0),
        unique_receptors=int(row.get("unique_receptors") or 0),
        best_score=None if row.get("best_score") is None else float(row.get("best_score")),
        avg_score=None if row.get("avg_score") is None else float(row.get("avg_score")),
        worst_score=None if row.get("worst_score") is None else float(row.get("worst_score")),
    )

def list_results(project_db, *, limit: int = 5000, offset: int = 0) -> list[DockingHitSummary]:
    # One summary per docked receptor-ligand PAIR: the selected pose (pose_rank=1 = best
    # score). Failed pairs never reach the table (they go to the run report), so every row
    # here is a completed result. Bounded: a screening has as many rows here as pairs docked.
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            +
            "WHERE r.pose_rank = 1 "
            "ORDER BY r.receptor_molecule_id ASC, r.ligand_molecule_id ASC, protocol_label ASC, r.pose_rank ASC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [_hit_summary(project_db, row) for row in rows]

def _hit_summary(project_db, row: dict[str, Any]) -> DockingHitSummary:
    return DockingHitSummary(
        result_id=int(row.get("result_id") or 0),
        complex_id=None if row.get("complex_id") is None else int(row.get("complex_id")),
        run_kind=str(row.get("run_kind") or "screening"),
        ligand_id=int(row.get("ligand_id") or 0),
        ligand_name=str(row.get("ligand_name") or ""),
        receptor_id=int(row.get("receptor_id") or 0),
        receptor_name=str(row.get("receptor_name") or ""),
        engine=str(row.get("engine") or ""),
        protocol_label=str(row.get("protocol_label") or ""),
        protocol_hash=str(row.get("protocol_hash") or ""),
        score=float(row.get("score") or 0.0),
        rmsd_vs_reference=_float_or_none(row.get("rmsd_vs_reference")),
        ligand_efficiency=_float_or_none(row.get("ligand_efficiency")),
        predicted_ki_m=_float_or_none(row.get("predicted_ki_m")),
        predicted_pki=_float_or_none(row.get("predicted_pki")),
        lipophilic_efficiency=_float_or_none(row.get("lipophilic_efficiency")),
        fit_quality=_float_or_none(row.get("fit_quality")),
        bei=_float_or_none(row.get("bei")),
        sei=_float_or_none(row.get("sei")),
        status=str(row.get("status") or ""),
        error=str(row.get("error") or ""),
        output_path=_coerce_project_path(project_db, row.get("output_path")),
        ligand_path=_coerce_project_path(project_db, row.get("ligand_path")),
        receptor_path=_coerce_project_path(project_db, row.get("receptor_path")),
        reference_ligand_path=_coerce_project_path(project_db, row.get("reference_ligand_path")),
        reference_receptor_path=_coerce_project_path(project_db, row.get("reference_receptor_path")),
        metadata=_decode_metadata(row.get("metadata_json")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )

def list_top_hits(
    project_db,
    *,
    limit: int = 25,
    receptor_id: int | None = None,
    ligand_id: int | None = None,
    only_completed: bool = True,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
    offset: int = 0,
) -> list[DockingHitSummary]:
    clauses = _hit_filter_clauses(
        receptor_id=receptor_id,
        ligand_id=ligand_id,
        only_completed=only_completed,
        score_lte=score_lte,
        metric_key=metric_key,
        metric_lte=metric_lte,
        metric_gte=metric_gte,
        metric_filters=metric_filters,
        run_kind=run_kind,
        protocol_hash=protocol_hash,
        exclude_run_kind=exclude_run_kind,
    )
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            + f"{where_clause} "
            "ORDER BY (r.score IS NULL) ASC, r.score ASC, r.created_at DESC, r.id DESC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [_hit_summary(project_db, row) for row in rows]


def get_hit(project_db, *, result_id: int) -> DockingHitSummary | None:
    """Hydrate one selected pose for the detail panel and molecular viewer."""
    rows = db_rows(
        project_db,
        query=(
            _hit_select_sql()
            + f"WHERE r.id = {int(result_id)} LIMIT 1"
        ),
    )
    return _hit_summary(project_db, rows[0]) if rows else None

def list_ligand_result_summaries(
    project_db,
    *,
    limit: int = 200,
    offset: int = 0,
    receptor_id: int | None = None,
    only_completed: bool = False,
    score_lte: float | None = None,
    metric_key: str | None = None,
    metric_lte: float | None = None,
    metric_gte: float | None = None,
    metric_filters: list[tuple[str, str, float]] | None = None,
    protocol_hash: str | None = None,
    exclude_run_kind: str | None = None,
) -> list[tuple[DockingHitSummary, int]]:
    """One row per ligand — its best pose plus how many poses it has — paginated.

    The results view used to pull every pose of every ligand and group them in Python, which
    is a full scan of the results table per open. Here SQLite groups, and the page is what
    the table actually shows.
    """
    clauses = _hit_filter_clauses(
        receptor_id=receptor_id,
        only_completed=only_completed,
        score_lte=score_lte,
        metric_key=metric_key,
        metric_lte=metric_lte,
        metric_gte=metric_gte,
        metric_filters=metric_filters,
        protocol_hash=protocol_hash,
        exclude_run_kind=exclude_run_kind,
    )
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            # MIN(r.score) makes SQLite take every other bare column from that same row,
            # so the summary is literally the best pose's row.
            _hit_select_sql(score_expr="MIN(r.score)", extra="COUNT(*) AS pose_count, ")
            + f"{where_clause} "
            "GROUP BY r.ligand_molecule_id "
            "ORDER BY (score IS NULL) ASC, score ASC, ligand_id ASC "
            f"LIMIT {max(1, int(limit))} OFFSET {max(0, int(offset))}"
        ),
    )
    return [(_hit_summary(project_db, row), int(row.get("pose_count") or 0)) for row in rows]

def list_result_protocols(
    project_db,
    *,
    receptor_id: int | None = None,
    exclude_run_kind: str | None = "redocking",
) -> list[tuple[str, str]]:
    """Distinct (protocol_hash, label) present in results — cheap combo source so the
    results view filters by protocol in SQL instead of scanning a client-side window."""
    clauses: list[str] = []
    if receptor_id is not None:
        clauses.append(f"r.receptor_molecule_id = {int(receptor_id)}")
    if exclude_run_kind:
        excluded = str(exclude_run_kind).strip().replace("'", "''")
        if excluded:
            clauses.append(f"COALESCE(JSON_EXTRACT(r.metrics, '$.run_kind'), 'screening') != '{excluded}'")
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            "SELECT DISTINCT "
            "COALESCE(JSON_EXTRACT(r.metrics, '$.protocol.hash'), '') AS protocol_hash, "
            "COALESCE(JSON_EXTRACT(r.metrics, '$.protocol.label'), '') AS protocol_label, "
            "r.engine AS engine "
            f"FROM {TABLE_DOCKING_RESULTS} r {where_clause}"
        ),
    )
    by_hash: dict[str, str] = {}
    for row in rows:
        phash = str(row.get("protocol_hash") or "")
        label = str(row.get("protocol_label") or row.get("engine") or "default")
        by_hash.setdefault(phash, label)
    return sorted(by_hash.items(), key=lambda item: item[1].lower())

def list_offtarget_rows(
    project_db,
    *,
    receptor_ids: list[int] | None = None,
    ligand_ids: list[int] | None = None,
    limit: int = 50000,
) -> list[dict[str, Any]]:
    """Flat per-pose rows for the ligand-centric off-target matrix: one dict per
    (ligand, receptor, pose) with score, ligand efficiency and pose path."""
    clauses: list[str] = []
    if receptor_ids:
        ids = ",".join(str(int(rid)) for rid in receptor_ids)
        clauses.append(f"r.receptor_molecule_id IN ({ids})")
    if ligand_ids:
        lids = ",".join(str(int(lid)) for lid in ligand_ids)
        clauses.append(f"r.ligand_molecule_id IN ({lids})")
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "r.ligand_molecule_id AS ligand_id, "
            "COALESCE(l.name, '') AS ligand_name, "
            "r.receptor_molecule_id AS receptor_id, "
            "COALESCE(rec.name, '') AS receptor_name, "
            "r.pose_rank AS pose_rank, "
            "r.score AS score, "
            "JSON_EXTRACT(r.metrics, '$.ligand_efficiency') AS ligand_efficiency, "
            "r.pose_path AS pose_path, "
            "rec.current_path AS receptor_path, "
            "l.stored_path AS ligand_path "
            f"FROM {TABLE_DOCKING_RESULTS} r "
            f"LEFT JOIN {TABLE_MOLECULES} l ON l.id = r.ligand_molecule_id "
            f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
            f"{where_clause} "
            "ORDER BY r.ligand_molecule_id ASC, r.receptor_molecule_id ASC, r.pose_rank ASC "
            f"LIMIT {max(1, int(limit))}"
        ),
    )
    return [
        {
            "ligand_id": int(row.get("ligand_id") or 0),
            "ligand_name": str(row.get("ligand_name") or ""),
            "receptor_id": int(row.get("receptor_id") or 0),
            "receptor_name": str(row.get("receptor_name") or ""),
            "pose_rank": int(row.get("pose_rank") or 1),
            "score": None if row.get("score") is None else float(row.get("score")),
            "ligand_efficiency": (
                None if row.get("ligand_efficiency") is None else float(row.get("ligand_efficiency"))
            ),
            "pose_path": _coerce_project_path(project_db, row.get("pose_path")),
            "receptor_path": _coerce_project_path(project_db, row.get("receptor_path")),
            "ligand_path": _coerce_project_path(project_db, row.get("ligand_path")),
        }
        for row in rows
    ]

def list_interactions(project_db, *, result_id: int) -> list[dict[str, Any]]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT interaction_type, residue, residue_index, distance, geometry, created_at "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            f"WHERE docking_result_id = {int(result_id)} "
            "ORDER BY interaction_type ASC, residue_index ASC, distance ASC"
        ),
    )
    return [
        {
            "interaction_type": str(row.get("interaction_type") or ""),
            "residue": str(row.get("residue") or ""),
            "residue_index": int(row.get("residue_index") or 0),
            "distance": _float_or_none(row.get("distance")),
            "geometry": _decode_metadata(row.get("geometry")),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]

def interaction_stats(project_db, *, result_ids: list[int] | None = None) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({int(value) for value in (result_ids or []) if int(value) > 0})
    id_clause = ""
    if ids:
        id_clause = f"AND docking_result_id IN ({','.join(str(value) for value in ids)}) "
    type_rows = db_rows(
        project_db,
        query=(
            "SELECT interaction_type AS label, "
            "COUNT(DISTINCT docking_result_id) AS hit_count, "
            "COUNT(*) AS interaction_count "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            "WHERE COALESCE(interaction_type, '') != 'none' "
            f"{id_clause}"
            "GROUP BY interaction_type "
            "ORDER BY hit_count DESC, interaction_count DESC, label ASC"
        ),
    )
    residue_rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "CASE "
            "WHEN COALESCE(residue, '') != '' AND COALESCE(residue_index, 0) > 0 THEN residue || residue_index "
            "WHEN COALESCE(residue, '') != '' THEN residue "
            "ELSE CAST(residue_index AS TEXT) "
            "END AS label, "
            "COUNT(DISTINCT docking_result_id) AS hit_count, "
            "COUNT(*) AS interaction_count "
            f"FROM {TABLE_INTERACTION_RESULTS} "
            "WHERE COALESCE(interaction_type, '') != 'none' "
            f"{id_clause}"
            "GROUP BY label "
            "ORDER BY hit_count DESC, interaction_count DESC, label ASC "
            "LIMIT 25"
        ),
    )
    return {
        "by_type": [
            {
                "label": str(row.get("label") or ""),
                "hit_count": int(row.get("hit_count") or 0),
                "interaction_count": int(row.get("interaction_count") or 0),
            }
            for row in type_rows
        ],
        "by_residue": [
            {
                "label": str(row.get("label") or ""),
                "hit_count": int(row.get("hit_count") or 0),
                "interaction_count": int(row.get("interaction_count") or 0),
            }
            for row in residue_rows
        ],
    }

def pivot_availability(project_db) -> dict[str, bool]:
    """What the Docking Results pivots have data for. A pivot is gated on being
    *populated*, never on whether a run happened: molecules and runs keep arriving, so
    "you have not redocked yet" has to stop being true the moment you have."""
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            f"EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} h "
            "WHERE COALESCE(JSON_EXTRACT(h.metrics, '$.run_kind'), 'screening') != 'redocking' "
            "LIMIT 1) AS hits, "
            f"EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} rd "
            "WHERE COALESCE(JSON_EXTRACT(rd.metrics, '$.run_kind'), 'screening') = 'redocking' "
            "LIMIT 1) AS redocked, "
            # MIN/MAX of the indexed receptor column rejects the common one-receptor case in
            # O(log N). Only projects that really have several receptors pay for grouping by
            # ligand to prove that at least one ligand is shared by two of them.
            f"CASE WHEN (SELECT COALESCE(MIN(receptor_molecule_id), -1) "
            f"FROM {TABLE_DOCKING_RESULTS}) = "
            f"(SELECT COALESCE(MAX(receptor_molecule_id), -1) "
            f"FROM {TABLE_DOCKING_RESULTS}) THEN 0 ELSE "
            f"EXISTS(SELECT 1 FROM {TABLE_DOCKING_RESULTS} ot "
            "GROUP BY ot.ligand_molecule_id "
            "HAVING MIN(ot.receptor_molecule_id) != MAX(ot.receptor_molecule_id) "
            "LIMIT 1) END AS shared_ligands"
        ),
    )
    row = rows[0] if rows else {}
    return {
        "hits": int(row.get("hits") or 0) > 0,
        "redocking": int(row.get("redocked") or 0) > 0,
        "offtarget": int(row.get("shared_ligands") or 0) > 0,
    }

def count_docked_pairs(
    project_db,
    *,
    engine: str = "",
    receptor_ids: list[int] | None = None,
    protocol_hash: str | None = None,
    run_kind: str | None = "screening",
) -> int:
    """How many receptor-ligand pairs already have a result. A COUNT, not the pair set:
    the run's skip-existing guard materializes the pairs because it has to compare them
    one by one; a preview only needs the number."""
    clauses = ["1=1"]
    normalized_engine = str(engine or "").strip().lower().replace("'", "''")
    if normalized_engine:
        clauses.append(f"LOWER(engine) = '{normalized_engine}'")
    if receptor_ids is not None:
        ids = sorted({int(value) for value in receptor_ids if int(value) > 0})
        # An explicit selection that resolves to nothing means "no receptors", not "all of
        # them": IN (NULL) is never true, so the count is 0 instead of the whole project.
        clauses.append(f"receptor_molecule_id IN ({','.join(str(value) for value in ids) or 'NULL'})")
    if run_kind:
        normalized_kind = str(run_kind).strip().replace("'", "''")
        clauses.append(f"COALESCE(JSON_EXTRACT(metrics, '$.run_kind'), 'screening') = '{normalized_kind}'")
    if protocol_hash:
        normalized_hash = str(protocol_hash).replace("'", "''")
        clauses.append(f"COALESCE(JSON_EXTRACT(metrics, '$.protocol.hash'), '') = '{normalized_hash}'")
    rows = db_rows(
        project_db,
        query=(
            "SELECT COUNT(*) AS pair_count FROM ("
            "SELECT DISTINCT receptor_molecule_id, ligand_molecule_id "
            f"FROM {TABLE_DOCKING_RESULTS} WHERE {' AND '.join(clauses)})"
        ),
    )
    return int((rows[0] if rows else {}).get("pair_count") or 0)

def list_receptor_result_summaries(project_db) -> list[ReceptorDockingSummary]:
    rows = db_rows(
        project_db,
        query=(
            "SELECT "
            "r.receptor_molecule_id AS receptor_id, "
            "COALESCE(rec.name, '') AS receptor_name, "
            # Count docked LIGANDS, not pose rows (each pair stores several poses).
            # Failed pairs are stored with score IS NULL.
            "COUNT(DISTINCT r.ligand_molecule_id) AS total_results, "
            "COUNT(DISTINCT CASE WHEN r.score IS NOT NULL THEN r.ligand_molecule_id END) AS completed_results, "
            "(COUNT(DISTINCT r.ligand_molecule_id) "
            "- COUNT(DISTINCT CASE WHEN r.score IS NOT NULL THEN r.ligand_molecule_id END)) AS failed_results, "
            "MIN(r.score) AS best_score, "
            "AVG(r.score) AS avg_score, "
            # The screening's ligand universe: how many ligands were docked against ANY
            # receptor. Compared against this receptor's own count it shows what's missing.
            f"(SELECT COUNT(DISTINCT ligand_molecule_id) FROM {TABLE_DOCKING_RESULTS}) AS expected_ligands "
            f"FROM {TABLE_DOCKING_RESULTS} r "
            f"LEFT JOIN {TABLE_MOLECULES} rec ON rec.id = r.receptor_molecule_id "
            "GROUP BY r.receptor_molecule_id, rec.name "
            "ORDER BY best_score ASC, total_results DESC, receptor_id ASC"
        ),
    )
    return [
        ReceptorDockingSummary(
            receptor_id=int(row.get("receptor_id") or 0),
            receptor_name=str(row.get("receptor_name") or ""),
            total_results=int(row.get("total_results") or 0),
            completed_results=int(row.get("completed_results") or 0),
            failed_results=int(row.get("failed_results") or 0),
            expected_ligands=int(row.get("expected_ligands") or 0),
            best_score=None if row.get("best_score") is None else float(row.get("best_score")),
            avg_score=None if row.get("avg_score") is None else float(row.get("avg_score")),
        )
        for row in rows
    ]


__all__ = [
    "count_docked_pairs",
    "delete_results_for_receptors",
    "docked_ligands_spec",
    "existing_result_pairs",
    "get_docking_results_stats",
    "get_hit",
    "interaction_stats",
    "molecule_scope_spec",
    "molecule_set_spec",
    "prepared_molecules_spec",
    "get_receptor_metadata_json",
    "get_molecule_rows_by_ids",
    "list_entity_rows",
    "list_complex_rows",
    "list_docking_result_rows",
    "list_interactions",
    "list_ligand_result_summaries",
    "list_offtarget_rows",
    "list_receptor_ids_in_set",
    "list_receptor_result_summaries",
    "list_result_protocols",
    "list_results",
    "list_top_hits",
    "persist_receptor_grid",
    "persist_prepared_updates",
    "pivot_availability",
    "project_db_path",
    "resolve_docking_output_dir",
    "resolve_storage_dir",
    "update_receptor_metadata_json",
]
