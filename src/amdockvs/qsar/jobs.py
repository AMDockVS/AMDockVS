from __future__ import annotations

import json
from pathlib import Path
from itertools import batched
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ms_flow.query import QuerySpec
from ms_flow.sinks import graph_sink
from amdockvs.core.jobs import job
from amdockvs.qsar.tasks import (
    _ALERT_COLUMNS,
    DESCRIPTOR_COLUMNS,
    calculate_alerts_batch,
    calculate_descriptor_batch,
    predict_rows_task,
    predict_shard_task,
    train_model_task,
)

from amdockvs.chemistry.structures import with_structures
from amdockvs.core.configuration import DEFAULT_LIGAND_BATCH_SIZE
from amdockvs.core.constants import (
    AMDOCKVS_OFFLOADABLE_EXECUTORS,
    TABLE_DESCRIPTOR_BLOCKS,
    TABLE_DESCRIPTORS,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
from amdockvs.core.paths import set_default_project_root
from amdockvs.models import (
    DescriptorBlockRecord,
    FingerprintRecord,
    MoleculeRecord,
    MoleculeRepresentation,
    QSARModelMemberRecord,
    QSARModelRecord,
    QSARPredictionRecord,
)
from amdockvs.core.worker_io import worker_file, worker_output_dir
from amdockvs.molecules.storage import ShardStore, as_store, store_from_config
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
    # The full RDKit 2D block the models train on; "missing" = no stored block.
    rdkit2d: bool = Field(default=False)
    # The molecules a training needs, when they are fewer than the scope.
    molecule_ids: list[int] | None = None


DESCRIPTOR_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "molecules",
            "model": MoleculeRecord,
            "columns": ["id", *DESCRIPTOR_COLUMNS],
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
        {
            "name": "descriptor_blocks",
            "model": DescriptorBlockRecord,
            "columns": ["molecule_id", "block", "values_json"],
            "write_mode": "upsert",
            "conflict_keys": ["molecule_id", "block"],
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
    elif params.only_missing and params.rdkit2d:
        resolved["id__not_in_subquery"] = QuerySpec(
            table=TABLE_DESCRIPTOR_BLOCKS, fields=("molecule_id",), filters={"block": "rdkit2d"}
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
    if params.molecule_ids is not None:
        resolved["id__in"] = [int(i) for i in params.molecule_ids]
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
        yield {"items": items, "fp": fp_spec, "rdkit2d": bool(params.rdkit2d)}


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
        {
            "name": "molecule_representations",
            "model": MoleculeRepresentation,
            "columns": ["molecule_id", "repr_type", "value"],
            "pk_field": "molecule_id",
            "write_mode": "upsert",
            "conflict_keys": ["molecule_id", "repr_type"],
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


class ShardPredictJobParams(BaseModel):
    model_path: str
    output_dir: str


@job(
    task=predict_shard_task,
    name="amdock_qsar_predict_shards_job",
    params_model=ShardPredictJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
)
def predict_shards_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """One shard per chunk, each scored into its own file: the predictions of a library stay
    beside it, by reference, until a cut promotes the best of them."""
    parsed = ShardPredictJobParams(**params)
    project_db = dict(config or {}).get("project_db")
    if project_db is None:
        raise ValueError("predict_shards_job requires project_db in config.")
    for row in ShardStore(project_db).iter_rows():
        yield {
            "shard_path": worker_file(row["path"]),
            "shard_id": int(row["id"]),
            "import_batch_id": row.get("import_batch_id"),
            "model_path": worker_file(parsed.model_path),
            "output_dir": worker_output_dir(parsed.output_dir),
        }


class TrainJobParams(BaseModel):
    """`data_path`: the labeled, split molecules (`build.train` writes it); `specs`: one per model."""
    data_path: str
    output_dir: str
    feature_kind: str
    feature_names: list[str] = Field(default_factory=list)
    fp_radius: int = 2
    fp_nbits: int = 2048
    specs: list[dict[str, Any]]


TRAIN_GRAPH_OUTPUT = graph_sink(
    nodes=(
        {"name": "qsar_models", "model": QSARModelRecord, "validate_model": False},
        {
            "name": "qsar_model_members",
            "model": QSARModelMemberRecord,
            "columns": ("model_id", "molecule_id", "role", "fold", "observed", "predicted", "probability",
                        "oof_predicted", "oof_probability"),
            "pk_field": "model_id",
            "validate_model": False,
        },
    ),
    relations=(
        {"source_node": "qsar_model_members", "source_ref_field": "model_ref",
         "target_node": "qsar_models", "fk_field": "model_id"},
    ),
)


def _train_label(payload: dict) -> str:
    return f"{payload['spec']['algorithm']} · {payload['spec']['endpoint']}"


@job(
    task=train_model_task,
    name="amdock_qsar_train_job",
    params_model=TrainJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=TRAIN_GRAPH_OUTPUT,
    task_label=_train_label,
    relative_paths=True,
)
def train_models_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """One task per model. Runs after the descriptor job it depends on, so the features are read
    here, once, and handed to every model by file; a molecule still without them is left out."""
    import numpy as np

    from amdockvs.qsar.features import design_matrix

    parsed = TrainJobParams(**params)
    project_db = dict(config or {}).get("project_db")
    if project_db is None:
        raise ValueError("train_models_job requires project_db in config.")
    split = dict(np.load(parsed.data_path, allow_pickle=False))
    with project_db.get_session() as session:
        kept, rows, names = design_matrix(
            session, split["ids"].tolist(), kind=parsed.feature_kind, names=parsed.feature_names,
            fp_radius=parsed.fp_radius, fp_nbits=parsed.fp_nbits,
        )
    if len(kept) < 2:
        raise ValueError(f"Only {len(kept)} of {split['ids'].size} labeled molecules have {parsed.feature_kind} "
                         "features; a model needs at least two.")
    at = {int(mid): i for i, mid in enumerate(split["ids"].tolist())}
    index = np.asarray([at[mid] for mid in kept], dtype=int)
    data_path = Path(parsed.data_path).with_name(Path(parsed.data_path).stem + "_design.npz")
    np.savez(
        data_path, x=np.asarray(rows, dtype=float if parsed.feature_kind == "rdkit2d" else np.uint8),
        ids=split["ids"][index], y=split["y"][index], is_test=split["is_test"][index],
        folds=split["folds"][index], groups=split["groups"][index] if split["groups"].size else split["groups"],
        names=np.asarray(names if parsed.feature_kind == "rdkit2d" else [], dtype=str),
    )
    for spec in parsed.specs:
        yield {"data_path": worker_file(data_path), "spec": spec, "output_dir": worker_output_dir(parsed.output_dir)}


class PredictRowsJobParams(DescriptorJobParams):
    model_id: int
    model_path: str


PREDICT_ROWS_OUTPUT = graph_sink(
    nodes=(
        {
            "name": "qsar_predictions",
            "model": QSARPredictionRecord,
            "columns": ["molecule_id", "model_id", "value", "confidence", "positive_probability"],
            "write_mode": "upsert",
            "conflict_keys": ["molecule_id", "model_id"],
            "validate_model": False,
        },
    ),
)


@job(
    task=predict_rows_task,
    name="amdock_qsar_predict_rows_job",
    params_model=PredictRowsJobParams,
    executor="compute",
    supported_executors=AMDOCKVS_OFFLOADABLE_EXECUTORS,
    output_spec=PREDICT_ROWS_OUTPUT,
)
def predict_rows_job(params: dict, config: dict | None = None) -> Iterator[dict]:
    """A model over the molecules of the database, a batch per task; scoring again replaces."""
    parsed = PredictRowsJobParams(**params)
    config_map = dict(config or {})
    if config_map.get("project_db") is None:
        raise ValueError("predict_rows_job requires project_db in config.")
    model_path = worker_file(parsed.model_path)
    for batch in _iter_descriptor_batches(store_from_config(config_map), parsed):
        yield {"items": batch["items"], "model_id": parsed.model_id, "model_path": model_path}
