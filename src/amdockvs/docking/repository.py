"""Entity-side queries for docking: scopes, molecule rows and preparation state.

What a docking run reads *before* it runs — which ligands and receptors are in scope,
whether they are prepared, which payloads they dock with. Result queries live in
:mod:`amdockvs.docking.results.repository`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ms_flow.core.database import ProjectStore
from ms_flow.payloads import with_payloads
from ms_flow.query import QuerySpec, db_count, db_pages, db_rows
from sqlmodel import select

from amdockvs.core.constants import (
    RESOURCE_DOCKING_RESULTS,
    TABLE_COMPLEXES,
    TABLE_DOCKING_RESULTS,
    TABLE_DOCKING_RUNS,
    TABLE_MOLECULES,
)
from amdockvs.core.payloads import molecule_payload_id, payload_store
from amdockvs.docking.preparation.state import docking_input_payload_id
from amdockvs.binding_sites.repository import active_site, set_active
from amdockvs.models import BindingSite, MoleculeRecord, prep_table
from amdockvs.models.molecules import sanitize_molecule_extra_data
from amdockvs.project.sets import (
    list_complex_set_ids,
    list_molecule_set_ids,
    molecule_set_spec,
    prepared_molecules_spec,
)


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


def inputs_dir(project_db) -> Path:
    """Where a docking job writes the receptors its programs read: a cache of payloads,
    `<id>.<kind>`, never stale since payload ids are never reused, under the project's cache/."""
    return project_db_path(project_db).parent / "cache" / "inputs"


def with_input_files(project_db, row: Mapping[str, Any], directory: str | Path) -> dict[str, Any]:
    """`row` with `input_path` (and `flex_path`, or "") as the files a docking program reads,
    each written once under `directory`: for receptors, which every pair of a job shares."""
    store = payload_store(project_db)
    flex = row.get("flex_payload_id")
    payload_id = docking_input_payload_id(row)
    return {
        **row,
        "input_payload_id": payload_id,
        "input_path": store.materialize(payload_id, directory),
        "flex_path": store.materialize(flex, directory) if flex else "",
    }


def with_input_bytes(project_db, rows: Iterable[Mapping[str, Any]]):
    """Ligand rows carrying the bytes they dock with (`ligand`, None without a structure), read a
    window at a time: what travels in the pair, so a ligand is never a file of the project."""
    keyed = (
        {**row, "input_payload_id": row.get("prepared_payload_id") or molecule_payload_id(row)}
        for row in rows
    )
    return with_payloads(project_db, keyed, {"input_payload_id": "ligand"})


def project_db_path(project_db) -> Path:
    db_path = getattr(project_db, "db_path", None)
    if db_path is None:
        raise ValueError("Docking repository requires project_db.db_path.")
    return Path(db_path).expanduser().resolve()


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
    model, molecule_column, payload_column = prep_table(role_type)
    states: dict[int, tuple] = {}  # molecule id -> (prepared payload, flex payload)
    sites: dict[int, BindingSite] = {}
    with project_db.get_session() as session:
        for offset in range(0, len(ids), _HYDRATE_ID_CHUNK):
            chunk = ids[offset:offset + _HYDRATE_ID_CHUNK]
            for molecule_id, payload_id, flex_id in session.exec(
                select(molecule_column, payload_column, getattr(model, "flex_payload_id", None))
                .where(molecule_column.in_(chunk))
                .where(model.family == str(engine))
            ).all():
                states[int(molecule_id)] = (payload_id, flex_id)
            # Active sites only: one per molecule at most.
            for site in session.exec(
                select(BindingSite)
                .where(BindingSite.molecule_id.in_(chunk))
                .where(BindingSite.is_active == True)  # noqa: E712 - SQL expression
            ).all():
                sites[int(site.molecule_id)] = site

    prepared_flag_key = f"prepared_{str(engine).strip().lower()}"
    grid_flag_key = f"grid_{str(engine).strip().lower()}"
    grid_payload_key = f"{grid_flag_key}_payload"
    for row in rows:
        molecule_id = int(row.get("id") or 0)
        if molecule_id <= 0:
            continue
        prepared_id, flex_id = states.get(molecule_id, (None, None))
        site = sites.get(molecule_id)
        payload = _binding_site_payload(site)
        prepared_ready = prepared_id is not None
        row["prepared_engine"] = prepared_ready
        row[prepared_flag_key] = prepared_ready
        row["prepared_payload_id"] = prepared_id
        row["flex_payload_id"] = flex_id if prepared_ready else None
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
    if normalized_kind != "ligand":
        # Docking needs coordinates: a sequence-only protein (no file yet) is out of scope.
        scope_filters.setdefault("has_3d", True)
    return normalized_kind, scope_filters, prepared_flag, grid_flag


def docked_ligands_spec(
    *,
    receptor_id: int,
    engine: str,
    run_kind: str | None = None,
    protocol_hash: str | None = None,
) -> QuerySpec:
    """Ligands already docked against this receptor — the "already computed" guard."""
    return QuerySpec(table=TABLE_DOCKING_RESULTS, fields=("ligand_molecule_id",), filters={
        "receptor_molecule_id": int(receptor_id),
        "docking_run_id__in_subquery": _runs_spec(engine, run_kind, protocol_hash),
    })


def _runs_spec(engine: str, run_kind: str | None, protocol_hash: str | None) -> QuerySpec:
    runs: dict[str, Any] = {"engine": str(engine).strip().lower()}
    if str(run_kind or "").strip():
        runs["run_kind"] = str(run_kind).strip()
    if str(protocol_hash or "").strip():
        runs["protocol_hash"] = str(protocol_hash).strip()
    return QuerySpec(table=TABLE_DOCKING_RUNS, fields=("id",), filters=runs)


def top_ligands_spec(
    *,
    receptor_id: int,
    engine: str,
    top_n: int,
    protocol_hash: str | None = None,
    ligand_scope: QuerySpec | None = None,
    max_score: float | None = None,
) -> QuerySpec:
    """The receptor's `top_n` best-scored ligands (pose 1) — the off-target funnel's input.

    `max_score` keeps only those at or below it (by-threshold selection; `top_n` is then the ceiling).

    Every screening run of the protocol counts, not only the latest: with skip_existing the
    target's earlier results live in older runs. `ligand_scope` ranks inside the current
    ligand scope, so a narrowed scope still yields N ligands, not N minus the ones outside it.
    """
    filters: dict[str, Any] = {
        "receptor_molecule_id": int(receptor_id),
        "pose_rank": 1,
        "score__is_not_null": True,
        "docking_run_id__in_subquery": _runs_spec(engine, "screening", protocol_hash),
    }
    if ligand_scope is not None:
        filters["ligand_molecule_id__in_subquery"] = ligand_scope
    if max_score is not None:
        filters["score__lte"] = float(max_score)
    return QuerySpec(table=TABLE_DOCKING_RESULTS, fields=("ligand_molecule_id",), filters=filters,
                     order=("score",), limit=int(top_n))


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
    extra_filters: Mapping[str, Any] | None = None,
) -> int:
    """How many rows match the scope — one COUNT(*), nothing materialized."""
    return db_count(
        project_db,
        molecule_scope_spec(
            entity_kind=entity_kind,
            engine=engine,
            set_id=set_id,
            filters=filters,
            extra_filters=extra_filters,
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
    fields: tuple[str, ...] = ("id", "stored_payload_id", "current_payload_id", "metadata_json"),
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
                "stored_payload_id",
                "current_payload_id",
                "extra_data",
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
    fields: tuple[str, ...] = ("id", "stored_payload_id", "current_payload_id", "metadata_json"),
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
        site = active_site(session, receptor)
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
        set_active(session, int(receptor_id), int(site.id))
        session.commit()


def list_receptor_ids_in_set(project_db, *, receptor_set_id: int) -> list[int]:
    return list_molecule_set_ids(project_db, int(receptor_set_id))


