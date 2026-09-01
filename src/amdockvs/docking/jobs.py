from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from pydantic import BaseModel, Field

from ms_flow.sinks import table_sink
from ms_flow.specs import InputSource
from ms_flow.tasking import JobSpec
from sqlmodel import delete

from amdockvs.configuration import batch_size_for
from amdockvs.api_common import project_root_from_output_dir, worker_file, worker_output_dir
from amdockvs.constants import (
    AMDOCKVS_LOCAL_EXECUTORS,
    DEFAULT_DOCKING_BATCH_SIZE,
    DEFAULT_VINA_BACKEND,
    DEFAULT_VINA_COMMAND,
)
from amdockvs.docking.service import (
    build_docking_pair,
    build_failed_docking_pair,
    grid_from_row,
    iter_docking_batches_from_rows,
)
from amdockvs.docking.repository import (
    delete_results_for_receptors,
    docked_ligands_spec,
    entity_ids,
    existing_result_pairs,
    iter_entity_rows,
    molecule_scope_spec,
    get_molecule_rows_by_ids,
    list_complex_rows,
    list_docking_result_rows,
    list_entity_rows,
    resolve_docking_output_dir,
)
from amdockvs.docking.engines import run_docking_chunk
from amdockvs.docking.interactions import collect_interaction_rows
from amdockvs.docking.protocols import DockingProtocolMetadata
import amdockvs.docking.autodock4  # noqa: F401  # registers the "autodock4" DOCK_RUNNER
import amdockvs.docking.gnina  # noqa: F401  # registers the "gnina" DOCK_RUNNER
from ms_flow.query import db_count
from amdockvs.models import DockingResultRecord, InteractionsResult


def _transport_docking_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Mark only the paths opened by a docking worker; receptor files are reusable."""
    chunk["output_dir"] = worker_output_dir(str(chunk["output_dir"]))
    for pair in list(chunk.get("pairs") or []):
        for key, cache in (
            ("ligand_path", False),
            ("ligand_source_path", False),
            ("receptor_path", True),
            ("reference_ligand_path", False),
            ("reference_receptor_path", True),
        ):
            logical = str(pair.get(key) or "")
            if logical:
                pair[f"{key}_logical"] = logical
                pair[key] = worker_file(logical, cache=cache)
        receptor = str(pair.get("receptor_path_logical") or "")
        if receptor:
            source = Path(receptor).expanduser()
            flex = source.with_name(f"{source.stem}__flex.pdbqt")
            if flex.is_file():
                pair["flex_receptor_path"] = worker_file(flex, cache=True)
    return chunk


def count_pending_docking_pairs(
    *,
    project_db,
    engine: str,
    preparation_engine: str,
    ligand_set_id: int | None,
    receptor_set_id: int | None,
    ligand_filters: Mapping[str, Any] | None,
    receptor_filters: Mapping[str, Any] | None,
    protocol_metadata: Mapping[str, Any] | None,
    skip_existing: bool,
) -> int:
    """Count exactly the receptor-ligand pairs that ``iter_docking_batches`` will emit.

    Receptors are few, so they are listed; ligands are a library, so they are only counted —
    once per receptor, with the "already docked" guard pushed into the query as a subquery.
    """
    ligand_scope = dict(ligand_filters or {})
    receptor_scope = dict(receptor_filters or {})
    ligand_scope["prepared_engine"] = True
    receptor_scope["prepared_engine"] = True
    receptor_ids = entity_ids(
        project_db,
        entity_kind="receptor",
        engine=preparation_engine,
        set_id=receptor_set_id,
        filters=receptor_scope,
    )
    if not receptor_ids:
        return 0

    def _count(extra_filters: Mapping[str, Any] | None = None) -> int:
        return db_count(
            project_db,
            molecule_scope_spec(
                entity_kind="ligand",
                engine=preparation_engine,
                set_id=ligand_set_id,
                filters=ligand_scope,
                extra_filters=extra_filters,
                order=(),
            ),
        )

    if not skip_existing:
        return _count() * len(receptor_ids)

    protocol_hash = str(
        DockingProtocolMetadata.from_mapping(protocol_metadata).as_metrics_payload().get("hash") or ""
    ).strip()
    return sum(
        _count({
            "id__not_in_subquery": docked_ligands_spec(
                receptor_id=receptor_id,
                engine=engine,
                run_kind="screening",
                protocol_hash=protocol_hash or None,
            )
        })
        for receptor_id in receptor_ids
    )


def count_pending_redocking_pairs(
    *,
    project_db,
    engine: str,
    complex_set_id: int | None,
    complex_ids: list[int] | None,
    purpose: str,
    protocol_metadata: Mapping[str, Any] | None,
    skip_existing: bool,
) -> int:
    """Count exactly the valid complex rows that ``iter_redocking_batches`` will emit."""
    complex_rows = list_complex_rows(project_db, set_id=complex_set_id, purpose=purpose)
    allowed = {int(value) for value in (complex_ids or ()) if int(value) > 0}
    if allowed:
        complex_rows = [row for row in complex_rows if int(row.get("id") or 0) in allowed]
    molecule_ids = [
        int(value)
        for row in complex_rows
        for value in (row.get("receptor_molecule_id"), row.get("ligand_molecule_id"))
        if int(value or 0) > 0
    ]
    molecules = get_molecule_rows_by_ids(project_db, molecule_ids, engine=engine)
    candidates = [
        row for row in complex_rows
        if int(row.get("id") or 0) > 0
        and int(row.get("receptor_molecule_id") or 0) in molecules
        and int(row.get("ligand_molecule_id") or 0) in molecules
    ]
    if not skip_existing:
        return len(candidates)
    protocol_hash = str(
        DockingProtocolMetadata.from_mapping(protocol_metadata).as_metrics_payload().get("hash") or ""
    ).strip()
    skipped_pairs = existing_result_pairs(
        project_db,
        engine=engine,
        run_kind="redocking",
        protocol_hash=protocol_hash or None,
    )
    return sum(
        1
        for row in candidates
        if (
            int(row.get("receptor_molecule_id") or 0),
            int(row.get("ligand_molecule_id") or 0),
        ) not in skipped_pairs
    )


def _project_root_from_db(project_db) -> Path | None:
    db_path = getattr(project_db, "db_path", None)
    if db_path is None:
        return None
    return Path(db_path).expanduser().resolve().parent


def _absolutize_pose_paths(rows: list[dict[str, Any]], project_db) -> list[dict[str, Any]]:
    """`pose_path` is stored project-relative; the interaction pass runs from an arbitrary CWD in a worker.
    Resolve it once here so every consumer (interactions, diagrams) gets a real file."""
    project_root = _project_root_from_db(project_db)
    for row in rows:
        row["pose_path"] = _resolve_project_path(row.get("pose_path"), project_root)
    return rows


def _resolve_project_path(raw: Any, project_root: Path | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


class DockingJobParams(BaseModel):
    output_dir: str | None = None
    batch_size: int = Field(default=DEFAULT_DOCKING_BATCH_SIZE, ge=1)
    engine: str = "vina"
    preparation_engine: str = "ad4"
    ligand_set_id: int | None = Field(default=None, ge=1)
    receptor_set_id: int | None = Field(default=None, ge=1)
    ligand_filters: dict[str, Any] = Field(default_factory=dict)
    receptor_filters: dict[str, Any] = Field(default_factory=dict)
    exhaustiveness: int = 8
    num_modes: int = 9
    box_center: tuple[float, float, float] | None = None
    box_size: tuple[float, float, float] | None = None
    scoring_function: str = "vina"
    vina_backend: str = DEFAULT_VINA_BACKEND
    vina_command: str = DEFAULT_VINA_COMMAND
    vina_cpu: int = Field(default=1, ge=0)
    seed: int = 0
    spacing: float = Field(default=0.375, gt=0.0)
    energy_range: float = Field(default=3.0, ge=0.0)
    min_rmsd: float = Field(default=1.0, ge=0.0)
    run_id: str = ""
    protocol_metadata: DockingProtocolMetadata = Field(default_factory=DockingProtocolMetadata)
    skip_existing: bool = True
    compute_diagram: bool = False
    diagram_format: str = "png"


class RedockingJobParams(BaseModel):
    output_dir: str | None = None
    batch_size: int = Field(default=DEFAULT_DOCKING_BATCH_SIZE, ge=1)
    engine: str = "vina"
    complex_set_id: int | None = Field(default=None, ge=1)
    complex_ids: list[int] = Field(default_factory=list)
    purpose: str = "redocking"
    exhaustiveness: int = 8
    num_modes: int = 9
    box_center: tuple[float, float, float] | None = None
    box_size: tuple[float, float, float] | None = None
    scoring_function: str = "vina"
    vina_backend: str = DEFAULT_VINA_BACKEND
    vina_command: str = DEFAULT_VINA_COMMAND
    vina_cpu: int = Field(default=1, ge=0)
    seed: int = 0
    spacing: float = Field(default=0.375, gt=0.0)
    energy_range: float = Field(default=3.0, ge=0.0)
    min_rmsd: float = Field(default=1.0, ge=0.0)
    run_id: str = ""
    protocol_metadata: DockingProtocolMetadata = Field(default_factory=DockingProtocolMetadata)
    skip_existing: bool = True
    compute_diagram: bool = False
    diagram_format: str = "png"


class InteractionJobParams(BaseModel):
    output_dir: str | None = None
    result_ids: list[int] = Field(default_factory=list)
    run_id: str = ""
    receptor_id: int | None = Field(default=None, ge=1)
    score_lte: float | None = None
    pose_rank: int | None = Field(default=1, ge=1)
    method: str = "ms_contactmap"
    chunk_size: int = Field(default=128, ge=1)
    replace_existing: bool = True


class DiagramJobParams(BaseModel):
    result_ids: list[int] = Field(default_factory=list)
    run_id: str = ""
    receptor_id: int | None = Field(default=None, ge=1)
    score_lte: float | None = None
    pose_rank: int | None = Field(default=1, ge=1)
    fmt: str = "png"
    chunk_size: int = Field(default=64, ge=1)
    replace_existing: bool = False


from amdockvs.docking.gnina import chunk_gpu_tokens as _extra_chunk_tokens


def iter_docking_batches(
    *,
    project_db,
    output_dir: str | Path,
    batch_size: int = DEFAULT_DOCKING_BATCH_SIZE,
    engine: str = "vina",
    ligand_set_id: int | None = None,
    receptor_set_id: int | None = None,
    ligand_filters: Mapping[str, Any] | None = None,
    receptor_filters: Mapping[str, Any] | None = None,
    exhaustiveness: int = 8,
    num_modes: int = 9,
    box_center: tuple[float, float, float] | None = None,
    box_size: tuple[float, float, float] | None = None,
    scoring_function: str = "vina",
    vina_backend: str = DEFAULT_VINA_BACKEND,
    vina_command: str = DEFAULT_VINA_COMMAND,
    vina_cpu: int = 1,
    seed: int = 0,
    spacing: float = 0.375,
    energy_range: float = 3.0,
    min_rmsd: float = 1.0,
    run_id: str = "",
    protocol_metadata: Mapping[str, Any] | DockingProtocolMetadata | None = None,
    preparation_engine: str | None = None,
    skip_existing: bool = True,
) -> Iterator[dict]:
    prep_engine = str(preparation_engine or engine)
    normalized_ligand_filters = dict(ligand_filters or {})
    normalized_receptor_filters = dict(receptor_filters or {})
    normalized_ligand_filters["prepared_engine"] = True
    normalized_receptor_filters["prepared_engine"] = True
    receptor_rows = list_entity_rows(
        project_db,
        entity_kind="receptor",
        engine=prep_engine,
        set_id=receptor_set_id,
        filters=normalized_receptor_filters,
        fields=(
            "id",
            "stored_path",
            "current_path",
            "input_format",
            "metadata_json",
            "prepared_engine_path",
            "prepared_files",
            "grid_engine_payload",
        ),
        order=("source", "source_index"),
    )
    receptors = [dict(row) for row in receptor_rows]
    if not receptors:
        return

    # Lightweight "already computed" guard: skip pairs that already have results (default),
    # or wipe prior results for these receptors so a forced re-dock replaces instead of duplicating.
    protocol_payload = (
        protocol_metadata.as_metrics_payload()
        if isinstance(protocol_metadata, DockingProtocolMetadata)
        else DockingProtocolMetadata.from_mapping(protocol_metadata).as_metrics_payload()
    )
    protocol_hash = str(protocol_payload.get("hash") or "").strip()
    if not skip_existing:
        delete_results_for_receptors(
            project_db,
            engine=engine,
            receptor_ids=[int(r.get("id") or 0) for r in receptors],
            protocol_hash=protocol_hash or None,
        )

    def ligands_for(receptor_id: int):
        """A fresh ligand stream per receptor, with the already-docked guard in the query.

        The skip used to be a set of every (receptor, ligand) pair already computed, held in
        memory for the whole job. Now the database answers it: `id NOT IN (docked for R)`.
        """
        extra_filters = None
        if skip_existing:
            extra_filters = {
                "id__not_in_subquery": docked_ligands_spec(
                    receptor_id=int(receptor_id),
                    engine=engine,
                    run_kind="screening",
                    protocol_hash=protocol_hash or None,
                )
            }
        return iter_entity_rows(
            project_db,
            entity_kind="ligand",
            engine=prep_engine,
            set_id=ligand_set_id,
            filters=normalized_ligand_filters,
            extra_filters=extra_filters,
            # Descriptors travel with the row: they are what feeds LLE/BEI/SEI in the worker.
            fields=("id", "stored_path", "current_path", "input_format", "metadata_json",
                    "prepared_engine_path", "prepared_files",
                    "heavy_atom_count", "mw", "logp", "tpsa", "hbd", "hba"),
            order=("id",),
            batch_size=batch_size_for("ligand"),
        )

    yield from iter_docking_batches_from_rows(
        ligands=ligands_for,
        receptors=receptors,
        output_dir=output_dir,
        batch_size=batch_size,
        exhaustiveness=exhaustiveness,
        num_modes=num_modes,
        box_center=box_center,
        box_size=box_size,
        scoring_function=scoring_function,
        vina_cpu=vina_cpu,
        vina_backend=vina_backend,
        vina_command=vina_command,
        seed=seed,
        spacing=spacing,
        energy_range=energy_range,
        min_rmsd=min_rmsd,
        run_id=str(run_id or ""),
        protocol_metadata=protocol_payload,
        engine=engine,
        preparation_engine=prep_engine,
    )


def iter_redocking_batches(
    *,
    project_db,
    output_dir: str | Path,
    batch_size: int = DEFAULT_DOCKING_BATCH_SIZE,
    engine: str = "vina",
    complex_set_id: int | None = None,
    complex_ids: list[int] | None = None,
    purpose: str = "redocking",
    exhaustiveness: int = 8,
    num_modes: int = 9,
    box_center: tuple[float, float, float] | None = None,
    box_size: tuple[float, float, float] | None = None,
    scoring_function: str = "vina",
    vina_backend: str = DEFAULT_VINA_BACKEND,
    vina_command: str = DEFAULT_VINA_COMMAND,
    vina_cpu: int = 1,
    seed: int = 0,
    spacing: float = 0.375,
    energy_range: float = 3.0,
    min_rmsd: float = 1.0,
    run_id: str = "",
    protocol_metadata: Mapping[str, Any] | DockingProtocolMetadata | None = None,
    skip_existing: bool = True,
) -> Iterator[dict]:
    complex_rows = list_complex_rows(
        project_db,
        set_id=complex_set_id,
        purpose=None if not str(purpose or "").strip() else str(purpose),
    )
    allowed_complex_ids = {int(value) for value in (complex_ids or []) if int(value) > 0}
    if allowed_complex_ids:
        complex_rows = [row for row in complex_rows if int(row.get("id") or 0) in allowed_complex_ids]
    if not complex_rows:
        return
    protocol_payload = (
        protocol_metadata.as_metrics_payload()
        if isinstance(protocol_metadata, DockingProtocolMetadata)
        else DockingProtocolMetadata.from_mapping(protocol_metadata).as_metrics_payload()
    )
    protocol_hash = str(protocol_payload.get("hash") or "").strip()
    skip_pairs = (
        existing_result_pairs(
            project_db,
            engine=engine,
            run_kind="redocking",
            protocol_hash=protocol_hash or None,
        )
        if skip_existing
        else set()
    )
    molecule_ids: list[int] = []
    for row in complex_rows:
        receptor_id = int(row.get("receptor_molecule_id") or 0)
        ligand_id = int(row.get("ligand_molecule_id") or 0)
        if receptor_id > 0:
            molecule_ids.append(receptor_id)
        if ligand_id > 0:
            molecule_ids.append(ligand_id)
    molecule_rows = get_molecule_rows_by_ids(project_db, molecule_ids, engine=engine)
    project_root = _project_root_from_db(project_db)
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    normalized_batch_size = max(1, int(batch_size))
    batch: list[dict[str, Any]] = []
    for complex_row in complex_rows:
        complex_id = int(complex_row.get("id") or 0)
        receptor_row = molecule_rows.get(int(complex_row.get("receptor_molecule_id") or 0))
        ligand_row = molecule_rows.get(int(complex_row.get("ligand_molecule_id") or 0))
        if complex_id <= 0 or receptor_row is None or ligand_row is None:
            continue
        # Prefer the frozen snapshot captured at pair creation; fall back to the ligand's
        # live path only for complexes imported before the snapshot existed.
        reference_ligand_path = _resolve_project_path(
            complex_row.get("reference_ligand_path")
            or ligand_row.get("current_path")
            or ligand_row.get("stored_path"),
            project_root,
        )
        reference_receptor_path = _resolve_project_path(
            complex_row.get("reference_receptor_path") or receptor_row.get("current_path") or receptor_row.get("stored_path"),
            project_root,
        )
        if (int(receptor_row.get("id") or 0), int(ligand_row.get("id") or 0)) in skip_pairs:
            continue  # already docked for this engine — lightweight skip
        pair_grid = grid_from_row(receptor_row, engine=engine)
        effective_center = (
            tuple(float(value) for value in box_center)
            if box_center is not None
            else tuple(float(value) for value in (pair_grid or {}).get("center", ()))
        )
        effective_size = (
            tuple(float(value) for value in box_size)
            if box_size is not None
            else tuple(float(value) for value in (pair_grid or {}).get("size", ()))
        )
        effective_spacing = float((pair_grid or {}).get("spacing") or spacing)
        if len(effective_center) != 3 or len(effective_size) != 3:
            batch.append(
                build_failed_docking_pair(
                    ligand_row=ligand_row,
                    receptor_row=receptor_row,
                    complex_id=complex_id,
                    run_kind="redocking",
                    reason=(
                        f"Complex {complex_id} requires explicit box_center/box_size "
                        "or a stored receptor grid."
                    ),
                    reference_ligand_path=reference_ligand_path,
                    reference_receptor_path=reference_receptor_path,
                )
            )
        else:
            try:
                batch.append(
                    build_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        exhaustiveness=exhaustiveness,
                        num_modes=num_modes,
                        engine=engine,
                        complex_id=complex_id,
                        run_kind="redocking",
                        box_center=effective_center,
                        box_size=effective_size,
                        spacing=effective_spacing,
                        reference_ligand_path=reference_ligand_path,
                        reference_receptor_path=reference_receptor_path,
                    )
                )
            except Exception as exc:
                batch.append(
                    build_failed_docking_pair(
                        ligand_row=ligand_row,
                        receptor_row=receptor_row,
                        complex_id=complex_id,
                        run_kind="redocking",
                        reason=str(exc),
                        reference_ligand_path=reference_ligand_path,
                        reference_receptor_path=reference_receptor_path,
                    )
                )
        if len(batch) >= normalized_batch_size:
            report_name = f"batch_{(complex_id or len(batch)):06d}_{len(batch)}.json"
            yield {
                "pairs": list(batch),
                "engine": str(engine),
                "output_dir": str(resolved_output_dir),
                "box_center": [],
                "box_size": [],
                "scoring_function": str(scoring_function),
                "vina_backend": str(vina_backend or "python"),
                "vina_command": str(vina_command or "vina"),
                "vina_cpu": int(vina_cpu),
                "seed": int(seed),
                "spacing": float(spacing),
                "energy_range": float(energy_range),
                "min_rmsd": float(min_rmsd),
                "run_id": str(run_id or ""),
                "protocol_metadata": protocol_payload,
                **_extra_chunk_tokens(engine, scoring_function),
                "report_name": report_name,
            }
            batch = []
    if batch:
        yield {
            "pairs": list(batch),
            "engine": str(engine),
            "output_dir": str(resolved_output_dir),
            "box_center": [],
            "box_size": [],
            "scoring_function": str(scoring_function),
            "vina_backend": str(vina_backend or "python"),
            "vina_command": str(vina_command or "vina"),
            "vina_cpu": int(vina_cpu),
            "seed": int(seed),
            "spacing": float(spacing),
            "energy_range": float(energy_range),
            "min_rmsd": float(min_rmsd),
            "run_id": str(run_id or ""),
            "protocol_metadata": protocol_payload,
            **_extra_chunk_tokens(engine, scoring_function),
            "report_name": f"batch_tail_{len(batch)}.json",
        }


class DockingPairsInput(InputSource):
    def iter_items(self, params: dict[str, Any], config: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise NotImplementedError("DockingPairsInput uses iter_chunks() directly.")

    def iter_chunks(
        self,
        params: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        params_map = dict(params or {})
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("DockingPairsInput requires project_db in config.")
        output_dir = str(params_map.get("output_dir") or "").strip()
        if not output_dir:
            raise ValueError("DockingPairsInput requires output_dir in params.")
        box_center = list(params_map.get("box_center") or [])
        box_size = list(params_map.get("box_size") or [])
        if box_center and len(box_center) != 3:
            raise ValueError(
                "DockingPairsInput requires box_center to have three values when provided."
            )
        if box_size and len(box_size) != 3:
            raise ValueError(
                "DockingPairsInput requires box_size to have three values when provided."
            )
        yield from iter_docking_batches(
            project_db=project_db,
            output_dir=output_dir,
            batch_size=int(params_map.get("batch_size", self.batch_size)),
            engine=str(params_map.get("engine") or "vina"),
            preparation_engine=str(params_map.get("preparation_engine") or params_map.get("engine") or "ad4"),
            ligand_set_id=params_map.get("ligand_set_id"),
            receptor_set_id=params_map.get("receptor_set_id"),
            ligand_filters=dict(params_map.get("ligand_filters") or {}),
            receptor_filters=dict(params_map.get("receptor_filters") or {}),
            exhaustiveness=int(params_map.get("exhaustiveness", 8)),
            num_modes=int(params_map.get("num_modes", 9)),
            box_center=None if not box_center else tuple(float(value) for value in box_center),
            box_size=None if not box_size else tuple(float(value) for value in box_size),
            scoring_function=str(params_map.get("scoring_function") or "vina"),
            vina_backend=str(params_map.get("vina_backend") or "python"),
            vina_command=str(params_map.get("vina_command") or "vina"),
            vina_cpu=int(params_map.get("vina_cpu", 1)),
            seed=int(params_map.get("seed", 0)),
            spacing=float(params_map.get("spacing", 0.375)),
            energy_range=float(params_map.get("energy_range", 3.0)),
            min_rmsd=float(params_map.get("min_rmsd", 1.0)),
            run_id=str(params_map.get("run_id") or ""),
            protocol_metadata=dict(params_map.get("protocol_metadata") or {}),
            skip_existing=bool(params_map.get("skip_existing", True)),
        )


class DockingVinaJobSpec(JobSpec):
    name = "amdock_docking_job"
    task_name = "amdock_run_vina_docking_batch"
    description = "Run AutoDock Vina docking for receptor-ligand pairs."
    params_model = DockingJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = table_sink(model=DockingResultRecord, write_mode="bulk")
    # Batch result rows per flush: flush_every=1 with the sink's >=1 MiB payload budget triggers
    # the framework's "small costly flushes" guardrail. Result rows are tiny and docking is slow
    # per pair, so 25 stays timely while avoiding the tiny-flush amplification.
    output_flush_every = 25
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def run_chunk(payload: dict):
        return run_docking_chunk(payload)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = DockingJobParams(**params)
        output_dir = resolve_docking_output_dir(params, config)
        enriched_params = dict(params)
        enriched_params["output_dir"] = str(output_dir)
        for chunk in DockingPairsInput(batch_size=parsed.batch_size, item_key="pairs").iter_chunks(
            params=enriched_params,
            config=config or {},
        ):
            # Inline diagrams: flag rides on the chunk so run_docking_chunk renders in the
            # same worker after docking (no service.py plumbing needed).
            if parsed.compute_diagram:
                chunk["compute_diagram"] = True
                chunk["diagram_format"] = parsed.diagram_format
            yield _transport_docking_chunk(chunk)


docking_job = DockingVinaJobSpec.to_job_definition()


def _interaction_result_rows(project_db, params: InteractionJobParams) -> list[dict[str, Any]]:
    rows = list_docking_result_rows(
        project_db,
        result_ids=params.result_ids,
        run_id=params.run_id,
        receptor_id=params.receptor_id,
        score_lte=params.score_lte,
        pose_rank=params.pose_rank,
    )
    result_ids = [int(row.get("id") or 0) for row in rows if int(row.get("id") or 0) > 0]
    if params.replace_existing and result_ids:
        with project_db.get_session() as session:
            session.exec(delete(InteractionsResult).where(InteractionsResult.docking_result_id.in_(result_ids)))
            session.commit()
    return _absolutize_pose_paths(rows, project_db)


def _iter_interaction_chunks(
    *,
    project_db,
    params: InteractionJobParams,
    output_dir: Path,
) -> Iterator[dict[str, Any]]:
    rows = _interaction_result_rows(project_db, params)
    chunk_size = max(1, int(params.chunk_size))
    if not rows:
        yield {"rows": [], "method": params.method, "output_dir": worker_output_dir(output_dir)}
        return
    for start in range(0, len(rows), chunk_size):
        chunk_rows = rows[start:start + chunk_size]
        for row in chunk_rows:
            row["pose_path"] = worker_file(row.get("pose_path"))
            metrics = dict(row.get("metrics") or {})
            metrics["receptor_path"] = worker_file(metrics.get("receptor_path"), cache=True)
            row["metrics"] = metrics
        yield {
            "rows": chunk_rows,
            "method": params.method,
            "output_dir": worker_output_dir(output_dir),
        }


def _relative_to_project(path: Path, project_root: Path | None) -> str:
    if project_root is not None:
        try:
            return str(path.resolve().relative_to(project_root.resolve()))
        except Exception:
            pass
    return str(path)


def _write_interaction_report(
    *,
    output_dir: Path,
    project_root: Path | None,
    result_id: int,
    row: Mapping[str, Any],
    method: str,
    interactions: list[dict[str, Any]],
) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"result_{int(result_id):09d}.interactions.json"
    payload = {
        "schema": "amdockvs.interactions.v1",
        "generated_at": datetime.now().isoformat(),
        "method": method,
        "docking_result_id": int(result_id),
        "receptor_molecule_id": int(row.get("receptor_molecule_id") or 0),
        "ligand_molecule_id": int(row.get("ligand_molecule_id") or 0),
        "engine": str(row.get("engine") or ""),
        "pose_rank": int(row.get("pose_rank") or 1),
        "score": row.get("score"),
        "pose_path": str(row.get("pose_path") or ""),
        "receptor_path": str((row.get("metrics") or {}).get("receptor_path") or ""),
        "interaction_count": len(interactions),
        "interactions": interactions,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")
    return _relative_to_project(path, project_root)


class InteractionJobSpec(JobSpec):
    name = "amdock_interactions_job"
    task_name = "amdock_compute_interactions"
    description = "Compute protein-ligand interactions for docking poses."
    params_model = InteractionJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = table_sink(model=InteractionsResult, write_mode="bulk")
    output_flush_every = 25
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def run_chunk(payload: dict):
        rows_out: list[dict[str, Any]] = []
        method = str(payload.get("method") or "ms_contactmap")
        output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
        project_root = project_root_from_output_dir(output_dir)
        for row in list(payload.get("rows") or []):
            result_id = int(row.get("id") or 0)
            if result_id <= 0:
                continue
            metrics = dict(row.get("metrics") or {})
            pose_path = str(row.get("pose_path") or "")
            receptor_path = str(metrics.get("receptor_path") or "")
            interactions = collect_interaction_rows(
                pose_path=pose_path,
                receptor_path=receptor_path,
                pose_rank=int(row.get("pose_rank") or 1),
            )
            json_path = _write_interaction_report(
                output_dir=output_dir,
                project_root=project_root,
                result_id=result_id,
                row=row,
                method=method,
                interactions=interactions,
            )
            if interactions:
                for interaction in interactions:
                    geometry = dict(interaction.get("geometry") or {})
                    geometry["json_path"] = json_path
                    interaction["geometry"] = geometry
                rows_out.extend(InteractionsResult.build_rows(result_id, interactions))
            else:
                rows_out.extend(
                    InteractionsResult.build_rows(
                        result_id,
                        [
                            {
                                "interaction_type": "none",
                                "residue": "",
                                "residue_index": 0,
                                "distance": None,
                                "geometry": {
                                    "json_path": json_path,
                                    "method": method,
                                    "interaction_count": 0,
                                },
                            }
                        ],
                    )
                )
        return rows_out

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("InteractionJobSpec requires project_db in config.")
        parsed = InteractionJobParams(**params)
        output_dir = resolve_docking_output_dir({"output_dir": parsed.output_dir}, config_map) / "interactions"
        yield from _iter_interaction_chunks(project_db=project_db, params=parsed, output_dir=output_dir)


interactions_job = InteractionJobSpec.to_job_definition()


def _diagram_result_rows(project_db, params: DiagramJobParams) -> list[dict[str, Any]]:
    return _absolutize_pose_paths(
        list_docking_result_rows(
            project_db,
            result_ids=params.result_ids,
            run_id=params.run_id,
            receptor_id=params.receptor_id,
            score_lte=params.score_lte,
            pose_rank=params.pose_rank,
        ),
        project_db,
    )


class DiagramJobSpec(JobSpec):
    name = "amdock_diagram_job"
    task_name = "amdock_render_interaction_diagrams"
    description = "Render 2D protein-ligand interaction diagrams for docking poses."
    params_model = DiagramJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = None  # writes PNG/SVG next to each pose; no DB rows
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def run_chunk(payload: dict):
        from amdockvs.docking.diagram import render_diagrams_for_result_rows

        render_diagrams_for_result_rows(
            list(payload.get("rows") or []),
            fmt=str(payload.get("fmt") or "png"),
            replace_existing=bool(payload.get("replace_existing")),
            output_dir=str(payload.get("output_dir") or "") or None,
        )
        return []

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("DiagramJobSpec requires project_db in config.")
        parsed = DiagramJobParams(**params)
        rows = _diagram_result_rows(project_db, parsed)
        output_dir = resolve_docking_output_dir({}, config_map)
        chunk_size = max(1, int(parsed.chunk_size))
        for start in range(0, len(rows), chunk_size) or [0]:
            chunk_rows = rows[start:start + chunk_size]
            for row in chunk_rows:
                row["pose_path"] = worker_file(row.get("pose_path"))
                metrics = dict(row.get("metrics") or {})
                metrics["receptor_path"] = worker_file(metrics.get("receptor_path"), cache=True)
                row["metrics"] = metrics
            yield {
                "rows": chunk_rows,
                "fmt": parsed.fmt,
                "replace_existing": parsed.replace_existing,
                "output_dir": worker_output_dir(output_dir),
            }


diagram_job = DiagramJobSpec.to_job_definition()


class RedockingVinaJobSpec(JobSpec):
    name = "amdock_redocking_job"
    task_name = "amdock_run_vina_redocking_batch"
    description = "Run AutoDock Vina redocking for explicit complex receptor-ligand pairs."
    params_model = RedockingJobParams
    executor = "compute"
    supported_executors = AMDOCKVS_LOCAL_EXECUTORS
    output_spec = table_sink(model=DockingResultRecord, write_mode="bulk")
    # Batch result rows per flush: flush_every=1 with the sink's >=1 MiB payload budget triggers
    # the framework's "small costly flushes" guardrail. Result rows are tiny and docking is slow
    # per pair, so 25 stays timely while avoiding the tiny-flush amplification.
    output_flush_every = 25
    store_results = False
    required = ()
    produces = ()

    @staticmethod
    def run_chunk(payload: dict):
        return run_docking_chunk(payload)

    @staticmethod
    def build_chunks(params: dict, config: dict | None = None) -> Iterator[dict]:
        parsed = RedockingJobParams(**params)
        config_map = dict(config or {})
        project_db = config_map.get("project_db")
        if project_db is None:
            raise ValueError("RedockingVinaJobSpec requires project_db in config.")
        output_dir = resolve_docking_output_dir(params, config)
        for chunk in iter_redocking_batches(
            project_db=project_db,
            output_dir=output_dir,
            batch_size=parsed.batch_size,
            engine=str(parsed.engine or "vina"),
            complex_set_id=parsed.complex_set_id,
            complex_ids=list(parsed.complex_ids or []),
            purpose=parsed.purpose,
            exhaustiveness=parsed.exhaustiveness,
            num_modes=parsed.num_modes,
            box_center=parsed.box_center,
            box_size=parsed.box_size,
            scoring_function=parsed.scoring_function,
            vina_backend=parsed.vina_backend,
            vina_command=parsed.vina_command,
            vina_cpu=parsed.vina_cpu,
            seed=parsed.seed,
            spacing=parsed.spacing,
            energy_range=parsed.energy_range,
            min_rmsd=parsed.min_rmsd,
            run_id=parsed.run_id,
            protocol_metadata=parsed.protocol_metadata,
            skip_existing=parsed.skip_existing,
        ):
            if parsed.compute_diagram:
                chunk["compute_diagram"] = True
                chunk["diagram_format"] = parsed.diagram_format
            yield _transport_docking_chunk(chunk)


redocking_job = RedockingVinaJobSpec.to_job_definition()
