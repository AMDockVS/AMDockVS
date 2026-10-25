from __future__ import annotations

import json
from pathlib import Path
from itertools import batched
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec
from ms_flow.sinks import graph_sink
from amdockvs.core.jobs import job
from amdockvs.qsar.tasks import _ALERT_COLUMNS, _DESCRIPTOR_COLUMNS, calculate_alerts_batch, calculate_descriptor_batch

from amdockvs.chemistry.structures import with_structures
from amdockvs.core.configuration import DEFAULT_LIGAND_BATCH_SIZE
from amdockvs.core.constants import (
    AMDOCKVS_OFFLOADABLE_EXECUTORS,
    TABLE_DESCRIPTORS,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
from amdockvs.core.paths import set_default_project_root
from amdockvs.models import FingerprintRecord, MoleculeRecord
from amdockvs.molecules.storage import as_store, store_from_config
from amdockvs.models.descriptors import FingerprintType
from amdockvs.project.sets import molecule_set_spec, prepared_molecules_spec


class DescriptorJobParams(BaseModel):
    batch_size: int = Field(default=DEFAULT_LIGAND_BATCH_SIZE, ge=1)
    molecule_set_id: int | None = Field(default=None, ge=1)
    molecule_filters: dict[str, Any] = Field(default_factory=dict)
    only_missing: bool = Field(default=False)
    # The alerts job: "missing" = PAINS never computed (JSON null, what a library import leaves).
    alerts: bool = Field(default=False)
    compute_fingerprints: bool = Field(default=False)
    fp_radius: int = Field(default=2, ge=1)
    fp_nbits: int = Field(default=2048, ge=64)


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


def scope_spec(params: DescriptorJobParams) -> QuerySpec:
    """The scope of a descriptor job, fully declared: it is the tool's only WHERE.

    Membership, preparation and "already has this fingerprint" live in other tables and go as
    subqueries; nothing library-sized is materialised in Python. Counting and iterating come from
    this same spec (`db_count` / `db_pages`), which is what keeps `total_chunks` and the feed
    from disagreeing.
    """
    filters = dict(params.molecule_filters or {})
    limit = filters.pop("_limit", None)

    resolved: dict[str, Any] = {"is_ligand": True, "excluded": False, "stored_payload_id__is_not_null": True}
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
    elif params.only_missing and params.alerts:
        resolved["pains_matches"] = "null"
    elif params.only_missing:
        resolved["mw__is_null"] = True

    has_3d_filter = filters.pop("has_3d", None)
    if has_3d_filter is not None:
        resolved["has_3d"] = bool(has_3d_filter)
    # workflow_filters emits "molecule_type__in"; accept "molecule_kind__in" as a legacy alias.
    molecule_type_in = filters.pop("molecule_type__in", None) or filters.pop("molecule_kind__in", None)
    if molecule_type_in:
        resolved["molecule_type__in"] = list(molecule_type_in)
    filters.pop("primary_context", None)
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
        fields=("id", "stored_payload_id", "current_payload_id"),
        filters=resolved,
        limit=None if limit is None else int(limit),
    )


def _iter_descriptor_batches(source, params: DescriptorJobParams) -> Iterator[dict[str, list[dict[str, Any]]]]:
    """`source` is the ligand store or the project db."""
    batch_size = max(1, int(params.batch_size))
    fp_spec = {"radius": int(params.fp_radius), "nbits": int(params.fp_nbits)} if params.compute_fingerprints else None
    store = as_store(source)
    rows = with_structures(store.project_db, store.iter_rows(scope_spec(params), batch_size=batch_size))
    for batch in batched(rows, batch_size):
        items = [
            {"molecule_id": int(row["id"]), "structure": row["structure"], "structure_kind": row["structure_kind"]}
            for row in batch
        ]
        yield {"items": items, "fp": fp_spec}


@job(
    task=calculate_descriptor_batch,
    name="amdock_calculate_molecule_descriptors_job",
    params_model=DescriptorJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=DESCRIPTOR_GRAPH_OUTPUT,
)
def calculate_molecule_descriptors_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    parsed = DescriptorJobParams(**params)
    config_map = dict(config or {})
    project_db = config_map.get("project_db")
    if project_db is None:
        raise ValueError("calculate_molecule_descriptors_job requires project_db in config.")
    yield from _iter_descriptor_batches(store_from_config(config_map), parsed)


ALERTS_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": ["id", *_ALERT_COLUMNS],
            "write_mode": "upsert",
            "conflict_keys": ["id"],
            "validate_model": False,
        },
    ),
)


@job(
    task=calculate_alerts_batch,
    name="amdock_calculate_molecule_descriptor_alerts_job",
    params_model=DescriptorJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=ALERTS_GRAPH_OUTPUT,
)
def calculate_molecule_alerts_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """PAINS and identity keys on demand: a library import leaves them out (~80 % of its chemistry)."""
    parsed = DescriptorJobParams(**{**params, "alerts": True, "compute_fingerprints": False})
    config_map = dict(config or {})
    if config_map.get("project_db") is None:
        raise ValueError("calculate_molecule_alerts_job requires project_db in config.")
    yield from _iter_descriptor_batches(store_from_config(config_map), parsed)
