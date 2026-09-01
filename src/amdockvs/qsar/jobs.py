from __future__ import annotations

import json
from pathlib import Path
from itertools import batched
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec, db_pages
from ms_flow.sinks import graph_sink
from ms_flow.tasking import job, task

from amdockvs.chemistry.descriptors import calculate_descriptor_rows
from amdockvs.api_common import worker_file
from amdockvs.constants import (
    AMDOCKVS_LOCAL_EXECUTORS,
    DEFAULT_DESCRIPTOR_BATCH_SIZE,
    TABLE_DESCRIPTORS,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
from amdockvs.molecule_paths import set_default_project_root
from amdockvs.molecule_paths import preferred_molecule_path
from amdockvs.models import FingerprintRecord, MoleculeRecord
from amdockvs.models.descriptors import FingerprintType
from amdockvs.scopes import molecule_set_spec, prepared_molecules_spec


class DescriptorJobParams(BaseModel):
    batch_size: int = Field(default=DEFAULT_DESCRIPTOR_BATCH_SIZE, ge=1)
    molecule_set_id: int | None = Field(default=None, ge=1)
    molecule_filters: dict[str, Any] = Field(default_factory=dict)
    only_missing: bool = Field(default=False)
    compute_fingerprints: bool = Field(default=False)
    fp_radius: int = Field(default=2, ge=1)
    fp_nbits: int = Field(default=2048, ge=64)


# Descriptor values are the source of truth on MoleculeRecord columns (the separate
# DescriptorVectorRecord BLOB is unused/incompatible here). Persistence goes through a
# MolSuite sink (engine-side, upsert), NOT a direct worker DB write — a second SQLite
# connection from a process/thread worker deadlocks against the engine's connection.
_DESCRIPTOR_COLUMNS = (
    "mw", "exact_mw", "logp", "hbd", "hba", "tpsa", "rotatable_bonds",
    "fragment_count", "ring_count", "aromatic_ring_count", "hetero_atom_count",
    "heavy_atom_count", "formal_charge", "fraction_csp3",
)

DESCRIPTOR_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": ["id", *_DESCRIPTOR_COLUMNS],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
        {
            "name": "fingerprints",
            "model": FingerprintRecord,
            "columns": ["molecule_id", "fp_type", "nbits", "radius", "fp_binary"],
            "write_mode": "upsert",
            "conflict_keys": ["molecule_id", "fp_type", "nbits", "radius"],
            "validate_model": False,
        },
    ),
)


@task(
    name="amdock_calculate_descriptor_batch",
    description="Calculate RDKit descriptors for one ligand batch.",
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
)
def calculate_descriptor_batch(payload: dict):
    # The worker only reads molecule files (set_default_project_root resolves stored paths)
    # and COMPUTES; it returns rows for the engine-side sink to persist. It does not open a
    # DB connection.
    rows = calculate_descriptor_rows(list(payload.get("items") or []), fingerprint=payload.get("fp"))
    return _descriptor_graph_payload(rows)


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


def scope_spec(params: DescriptorJobParams) -> QuerySpec:
    """The scope of a descriptor job, fully declared: it is the tool's only WHERE.

    Membership, preparation and "already has this fingerprint" live in other tables and go as
    subqueries; nothing library-sized is materialised in Python. Counting and iterating come from
    this same spec (`db_count` / `db_pages`), which is what keeps `total_chunks` and the feed
    from disagreeing.
    """
    filters = dict(params.molecule_filters or {})
    limit = filters.pop("_limit", None)

    resolved: dict[str, Any] = {"is_ligand": True, "excluded": False, "stored_path__ne": ""}
    in_specs: list[QuerySpec] = []
    if params.molecule_set_id is not None:
        in_specs.append(molecule_set_spec(int(params.molecule_set_id)))
    if params.only_missing and params.compute_fingerprints:
        # "missing" = lacks this fingerprint, so we can add FPs to already-described ligands.
        fp_type = FingerprintType.ECFP6 if params.fp_radius >= 3 else FingerprintType.ECFP4
        resolved["id__not_in_subquery"] = QuerySpec(
            table=TABLE_FINGERPRINTS,
            fields=("molecule_id",),
            filters={"fp_type": fp_type, "nbits": int(params.fp_nbits), "radius": int(params.fp_radius)},
        )
    elif params.only_missing:
        resolved["mw__is_null"] = True

    has_3d_filter = filters.pop("has_3d", None)
    if has_3d_filter is not None:
        resolved["has_3d"] = bool(has_3d_filter)
    # workflow_filters emits "molecule_type__in"; accept "molecule_kind__in" as a legacy alias.
    molecule_type_in = filters.pop("molecule_type__in", None) or filters.pop("molecule_kind__in", None)
    if molecule_type_in:
        resolved["molecule_type__in"] = list(molecule_type_in)
    primary_context = filters.pop("primary_context", None)
    if primary_context is not None:
        resolved["primary_context"] = primary_context
    filters.pop("primary_role", None)
    prepared_filter = filters.pop("prepared", None)
    prep_engine = str(filters.pop("prepared_engine_key", "") or "ad4").strip().lower()
    if prepared_filter is not None:
        in_specs.append(
            prepared_molecules_spec(role_type="ligand", engine=prep_engine, is_ready=bool(prepared_filter))
        )
    if in_specs:
        resolved["id__in_subquery"] = in_specs
    for key in ("n_atoms__gt", "n_atoms__gte", "n_atoms__lt", "n_atoms__lte", "mw__gte", "mw__lte", "fragment_count"):
        if key in filters:
            resolved[key] = filters[key]

    return QuerySpec(
        table=TABLE_MOLECULES,
        fields=("id", "stored_path", "current_path"),
        filters=resolved,
        limit=None if limit is None else int(limit),
    )


def _iter_descriptor_batches(project_db, params: DescriptorJobParams) -> Iterator[dict[str, list[dict[str, Any]]]]:
    batch_size = max(1, int(params.batch_size))
    fp_spec = {"radius": int(params.fp_radius), "nbits": int(params.fp_nbits)} if params.compute_fingerprints else None
    rows = db_pages(project_db, scope_spec(params), page_size=batch_size)
    for batch in batched(rows, batch_size):
        items = [
            {
                "molecule_id": int(row["id"]),
                "stored_path": worker_file(preferred_molecule_path(row) or row.get("stored_path")),
            }
            for row in batch
        ]
        yield {"items": items, "fp": fp_spec}


@job(
    task=calculate_descriptor_batch,
    name="amdock_calculate_molecule_descriptors_job",
    params_model=DescriptorJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_LOCAL_EXECUTORS,
    output_spec=DESCRIPTOR_GRAPH_OUTPUT,
)
def calculate_molecule_descriptors_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    parsed = DescriptorJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("calculate_molecule_descriptors_job requires project_db in config.")
    yield from _iter_descriptor_batches(project_db, parsed)
