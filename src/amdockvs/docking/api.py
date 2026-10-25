from __future__ import annotations

import contextlib
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import exists, or_
from sqlmodel import select
from ms_flow.payloads import scratch_file
from ms_flow.query import db_count
from ms_flow.selection import Selection

import amdockvs.docking.results.repository as results_repo
from amdockvs.core.configuration import DEFAULT_DOCKING_BATCH_SIZE, app_config, batch_size_for
from amdockvs.core.constants import (
    DEFAULT_LOCAL_CPU_EXECUTOR,
    DEFAULT_VINA_BACKEND,
    DEFAULT_VINA_COMMAND,
)
from amdockvs.docking.repository import (
    count_entity_rows,
    entity_ids,
    get_molecule_rows_by_ids,
    list_complex_rows,
    list_entity_rows,
    list_receptor_ids_in_set,
)


class MissingLigand3DError(RuntimeError):
    """Ligand preparation was asked for ligands with no 3D coordinates. `count` is how many,
    so a caller can offer to generate them instead of only reporting it."""

    def __init__(self, message: str, *, count: int) -> None:
        super().__init__(message)
        self.count = int(count)


# How many offending ids a requirement check reports. They exist to be read by a human in a
# dialog, and the caller gets the exact count separately.
MISSING_SAMPLE_SIZE = 10
from amdockvs.docking.engines.programs import (
    DockingEngineConfig,
    DockingProgramSpec,
    VINA_PROGRAM,
    get_docking_program,
    list_docking_programs,
)
from amdockvs.docking.jobs import (
    count_pending_docking_pairs,
    count_pending_redocking_pairs,
    DockingJobParams,
    RedockingJobParams,
    docking_job,
    redocking_job,
)
from amdockvs.docking.results.jobs import (
    DiagramJobParams,
    InteractionJobParams,
    diagram_job,
    interactions_job,
)
from amdockvs.docking.protocols import DockingProtocolMetadata
from amdockvs.docking.shard_jobs import (
    DockShardsJobParams,
    dock_shards_job,
    recover_ranked_hits,
    recoverable_ranked_runs,
    select_hits as select_shard_hits,
)
from amdockvs.molecules.storage import has_shards
from amdockvs.screening.materialize import PAYLOAD_LIGHT
from amdockvs.screening.campaign import list_targets, live_target_progress
from amdockvs.docking.preparation.jobs import (
    PreparationJobParams,
    PrepareLigandShardsJobParams,
    prepare_ligand_shards_job,
    prepare_ligands_job,
    prepare_receptors_job,
)
from amdockvs.core.normalize import PathLike
from amdockvs.molecules.scopes import MoleculeScope, is_molecule_scope, scope_payload
from amdockvs.binding_sites.repository import active_site as _active_site, site_payload
from amdockvs.docking.residues import residues_in_box
from amdockvs.core.payloads import molecule_payload_id, read_structure
from amdockvs.io.formats import as_pdb
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.models import BindingSite, MoleculeRecord, ReceptorPrep, prep_table
from amdockvs.project.sets import ComplexSetRef, MoleculeSetRef, prepared_molecules_spec
from amdockvs.workflows.rules import apply_workflow_filters


def _validated_engine_config(
    program: DockingProgramSpec,
    overrides: dict[str, Any] | None,
    **legacy,
) -> dict[str, Any]:
    values = (
        {**legacy, **dict(overrides or {})}
        if program.config_model is DockingEngineConfig
        else dict(overrides or {})
    )
    return program.validate_config(values)


# The engine knobs every docking job carries as flat fields, with the type each one is stored as.
_ENGINE_FIELDS = {
    "exhaustiveness": int,
    "num_modes": int,
    "scoring_function": str,
    "vina_backend": str,
    "vina_command": str,
    "vina_cpu": int,
    "seed": int,
    "spacing": float,
    "energy_range": float,
    "min_rmsd": float,
}


def _engine_fields(resolved: dict[str, Any], knobs: dict[str, Any]) -> dict[str, Any]:
    """The flat engine fields of a job: the validated config wins, the call's own knobs fill in
    what the program's config model does not know."""
    return {name: cast(resolved.get(name, knobs[name])) for name, cast in _ENGINE_FIELDS.items()}


def _requirements_message(program: str, check: Mapping[str, Any]) -> str:
    """What blocks a docking run, in words: how many of what, and which ids (a sample)."""
    counts, missing = dict(check.get("counts") or {}), dict(check.get("missing") or {})
    lines = []
    if not counts.get("ligands_total"):
        lines.append("no ligands in the scope")
    if not counts.get("receptors_total"):
        lines.append("no receptors in the scope")
    # `missing` holds capped samples, so the real numbers come from `counts`.
    for count, total, what, sample in (
        ("ligands_missing", "ligands_total", "ligands are not prepared", "ligands_prepared"),
        ("receptors_missing", "receptors_total", "receptors are not prepared", "receptors_prepared"),
        ("receptor_grids_missing", "receptors_total", "receptors have no grid box", "receptor_binding_sites"),
    ):
        if counts.get(count):
            ids = ", ".join(str(i) for i in missing.get(sample) or [])
            more = ", …" if counts[count] > len(missing.get(sample) or []) else ""
            lines.append(f"{counts[count]} of {counts[total]} {what} (ids {ids}{more})")
    return (
        f"Docking with {program} can't start: " + "; ".join(lines) + ". "
        "Prepare them (the Prep Status tab shows why one failed) or exclude them, and run again."
    )


@dataclass
class DockingAPI:
    runtime: Any

    def _project_db(self):
        self.runtime._require_active_project()
        return self.runtime.workspace.project_db

    def list_programs(self) -> tuple[DockingProgramSpec, ...]:
        return list_docking_programs()

    def live_campaign_progress(
        self,
        *,
        job_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[int, dict[str, int]]:
        """Current per-receptor totals of what is written."""
        self.runtime._require_active_project()
        # ponytail: the engine does not list the chunks a job has in flight, so the totals count
        # what is written; `job_ids` comes back when it does.
        return live_target_progress(list_targets(self.runtime.workspace.project_db), [])

    def recoverable_ranked_runs(self) -> list[dict[str, Any]]:
        """Interrupted top-N campaigns recoverable from completed result shards."""
        context = self.runtime._require_active_project()
        return recoverable_ranked_runs(
            self.runtime.workspace.project_db,
            project_root=context.path,
        )

    def select_hits(
        self,
        *,
        run_id: str,
        protocol_hash: str,
        filters: dict[str, Any] | None = None,
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Select a recomputable Top-N from the durable HTP result dataset."""
        context = self.runtime._require_active_project()
        return select_shard_hits(
            self.runtime.workspace.project_db,
            project_root=context.path,
            run_id=run_id,
            protocol_hash=protocol_hash,
            filters=filters,
            top_n=top_n,
        )

    def recover_ranked_hits(
        self,
        *,
        run_id: str,
        protocol_hash: str,
        top_n: int,
        threshold: float = 0.0,
    ) -> dict[str, int]:
        """Materialize the best available N without submitting docking work."""
        context = self.runtime._require_active_project()
        return recover_ranked_hits(
            self.runtime.workspace.project_db,
            project_root=context.path,
            run_id=run_id,
            protocol_hash=protocol_hash,
            top_n=top_n,
            threshold=threshold,
        )

    def preparation_summary(
            self,
            scope: MoleculeScope,
            *,
            role_type: str,
            engine: str = "ad4",
    ) -> dict[str, int]:
        """Return total/ready/failed preparation counts for a molecule scope."""
        self.runtime._require_active_project()
        normalized_role = str(role_type or "").strip().lower()
        normalized_engine = str(engine or "ad4").strip().lower()
        if normalized_role not in {"ligand", "receptor"}:
            raise ValueError("role_type must be 'ligand' or 'receptor'.")
        # Count each side alone first: a family with no state row of that kind anywhere needs
        # no join with the scope (the common case before the first preparation).
        molecules = self.runtime.molecules
        project_db = self.runtime.workspace.project_db
        flags = {"is_ligand" if normalized_role == "ligand" else "is_receptor": True, "prepared_engine_key": normalized_engine}

        def in_scope(ready: bool) -> int:
            states = prepared_molecules_spec(role_type=normalized_role, engine=normalized_engine, is_ready=ready)
            if not db_count(project_db, states):
                return 0
            return molecules.count(molecules.filter(scope, filters={**flags, "prepared": ready}))

        total = molecules.count(scope)
        return {"total": total, "ready": in_scope(True) if total else 0, "failed": in_scope(False) if total else 0}

    def unprepared_molecules_clause(self, *, role_type: str, engines: list[str] | tuple[str, ...]):
        """Opaque SmartTable clause selecting molecules not ready for every engine family."""
        self.runtime._require_active_project()
        role = str(role_type or "").strip().lower()
        if role not in {"ligand", "receptor"}:
            raise ValueError("role_type must be 'ligand' or 'receptor'.")
        normalized_engines = tuple(
            dict.fromkeys(str(value or "").strip().lower() for value in engines if str(value or "").strip()))
        project_db = self.runtime.workspace.project_db
        if any(
            not db_count(project_db, prepared_molecules_spec(role_type=role, engine=engine))
            for engine in normalized_engines
        ):
            return None  # a family nothing is ready for leaves every row in: no anti-join to run
        # NOT IN, not a correlated NOT EXISTS: DuckDB (1 thread, 1 GB) hash-joins the whole
        # table for the latter (4 s and GBs of spill for one page); SQLite is faster with it too.
        model, molecule_column, payload_column = prep_table(role)
        missing = [
            MoleculeRecord.id.not_in(
                select(molecule_column).where(model.family == engine, payload_column.is_not(None))
            )
            for engine in normalized_engines
        ]
        if not missing:
            return None
        return missing[0] if len(missing) == 1 else or_(*missing)

    def delete_results(self, result_ids: list[int] | tuple[int, ...], *, with_siblings: bool = False) -> int:
        """Delete results, their interactions and files. `with_siblings`: every pose of the same
        receptor, ligand, protocol and run as each id goes too — what a Results ligand row
        (its best pose) stands for."""
        self.runtime._require_active_project()
        from amdockvs.project.deletion import delete_docking_results, delete_results_where

        project_db = self.runtime.workspace.project_db
        if not with_siblings:
            return delete_docking_results(project_db, result_ids)
        from sqlalchemy.orm import aliased

        from amdockvs.models import DockingResultRecord

        seed = aliased(DockingResultRecord)
        return delete_results_where(project_db, exists().where(
            seed.id.in_([int(i) for i in result_ids]),
            seed.receptor_molecule_id == DockingResultRecord.receptor_molecule_id,
            seed.ligand_molecule_id == DockingResultRecord.ligand_molecule_id,
            seed.docking_run_id == DockingResultRecord.docking_run_id,
        ))

    @staticmethod
    def get_program_spec(program: str | None = None) -> DockingProgramSpec:
        return get_docking_program(program)

    @staticmethod
    def _entity_filters_for_workflow(
            scope: dict[str, Any],
            *,
            role: str,
            workflow: str = VINA_PROGRAM.workflow_key,
    ) -> dict[str, Any]:
        filters = dict(scope.get("filters") or {})
        filters.setdefault("excluded", False)
        filters = apply_workflow_filters(filters, workflow=workflow, role=role)
        if scope.get("limit") is not None:
            filters["_limit"] = scope.get("limit")
        return filters

    def _program_scope_filters(
            self,
            scope: dict[str, Any],
            *,
            role: str,
            program: DockingProgramSpec,
    ) -> dict[str, Any]:
        filters = self._entity_filters_for_workflow(scope, role=role, workflow=program.workflow_key)
        if scope.get("limit") is not None:
            filters["_limit"] = scope.get("limit")
        return filters

    def _submit_preparation_job(
            self,
            job_def,
            *,
            params: PreparationJobParams,
            executor_name: str,
            depends_on: list[str] | None = None,
    ) -> str:
        self.runtime._require_active_project()
        return self.runtime.submit_job(
            job_def,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def check_ligand_preparation_required(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
    ) -> dict[str, Any]:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        ligand_set_ref = None if ligand_set is None or is_molecule_scope(ligand_set) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="prepare_ligands_check_input")
        ligand_scope = scope_payload(ligand_set) if is_molecule_scope(ligand_set) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        project_db = self.runtime.workspace.project_db
        set_id = None if ligand_set_ref is None else int(ligand_set_ref.id)
        prep_engine = program_spec.preparation_engine
        total = count_entity_rows(
            project_db, entity_kind="ligand", engine=prep_engine, set_id=set_id, filters=ligand_filters
        )
        # The offenders are only ever counted or shown to a user, so fetch a sample, not the
        # whole list: on a million-ligand library the full list is both the slow part and an
        # unreadable error message.
        # The program says whether it needs coordinates: Meeko does, DiffDock docks from the
        # SMILES. Asking anyway would block a program on a precondition it does not have.
        missing_has_3d: list[int] = []
        missing_total = 0
        if program_spec.requires_ligand_3d:
            missing_has_3d = entity_ids(
                project_db,
                entity_kind="ligand",
                engine=prep_engine,
                set_id=set_id,
                filters={**ligand_filters, "has_3d": False},
                limit=MISSING_SAMPLE_SIZE + 1,
            )
            missing_total = (
                len(missing_has_3d)
                if len(missing_has_3d) <= MISSING_SAMPLE_SIZE
                else count_entity_rows(
                    project_db,
                    entity_kind="ligand",
                    engine=prep_engine,
                    set_id=set_id,
                    filters={**ligand_filters, "has_3d": False},
                )
            )
        return {
            "ready": bool(total) and not missing_has_3d,
            "operation": program_spec.operation_name("prepare_ligands"),
            "missing": {
                "ligands_has_3d": missing_has_3d[:MISSING_SAMPLE_SIZE],
            },
            "required_jobs": {
                "ligands_has_3d": "runtime.chemistry.generate_3d_ligands(...)",
            },
            "counts": {
                "ligands_total": total,
                "ligands_missing_has_3d": missing_total,
                "ligands_ready_has_3d": total - missing_total,
            },
        }

    def prepare_ligands(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligands: MoleculeSetRef | MoleculeScope | Selection[MoleculeRecord] | int | None = None,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            batch_size: int | None = None,  # None -> settings (batch_sizes.ligand)
            force: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
    ) -> str:
        if ligands is not None:
            if ligand_set is not None:
                raise ValueError("Pass either ligands or ligand_set, not both.")
            ligand_set = ligands
        program_spec = self.get_program_spec(program)
        if not program_spec.requires_ligand_preparation:
            raise ValueError(f"Docking program '{program_spec.key}' does not require ligand preparation.")
        if ligand_set is None:
            self.runtime._require_active_project()
            if has_shards(self.runtime.workspace.project_db):
                return self.prepare_ligand_shards(
                    program=program_spec.key,
                    force=force,
                    executor_name=executor_name,
                    depends_on=depends_on,
                )
        # In a deferred pipeline (workflow) prepare is submitted up-front to WAIT for the 3D job,
        # so a submit-time has_3d gate would always fail — pass check_required=False to skip it;
        # the prepare job's chunk build runs after its dependencies and sees the 3D ligands.
        if check_required:
            requirement_check = self.check_ligand_preparation_required(program=program_spec.key, ligand_set=ligand_set)
            if not bool(requirement_check.get("ready")):
                counts = requirement_check.get("counts") or {}
                sample = list((requirement_check.get("missing") or {}).get("ligands_has_3d") or [])
                total = int(counts.get("ligands_total") or 0)
                if total <= 0:
                    raise RuntimeError("prepare_ligands did not resolve any ligands to process.")
                missing_count = int(counts.get("ligands_missing_has_3d") or len(sample))
                shown = ", ".join(str(value) for value in sample)
                if missing_count > len(sample):
                    shown = f"{shown}, … (+{missing_count - len(sample)})"
                raise MissingLigand3DError(
                    f"Missing has_3d for {missing_count} ligand(s): {shown}. "
                    "Run runtime.chemistry.generate_3d_ligands(...) first.",
                    count=missing_count,
                )
        ligand_set_ref = None if ligand_set is None or is_molecule_scope(ligand_set) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="prepare_ligands_input")
        ligand_scope = scope_payload(ligand_set) if is_molecule_scope(ligand_set) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        params = PreparationJobParams(
            batch_size=batch_size,
            ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
            ligand_filters=ligand_filters,
            engine=program_spec.preparation_engine,
            force=force,
        )
        return self._submit_preparation_job(
            prepare_ligands_job,
            params=params,
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def prepare_ligand_shards(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            force: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
    ) -> str:
        """`prepare_ligands` for a sharded library: the unit of work is a shard, not a row.

        No scope argument on purpose — a sharded library has no rows to select from, so the
        whole inventory is the scope and narrowing it happened at import. One shard is one task.
        """
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        if not program_spec.requires_ligand_preparation:
            raise ValueError(f"Docking program '{program_spec.key}' does not require ligand preparation.")
        if not program_spec.supports_shards:
            raise ValueError(f"Docking program '{program_spec.key}' does not support sharded libraries.")
        params = PrepareLigandShardsJobParams(
            engine=program_spec.preparation_engine,
            force=force,
        )
        return self.runtime.submit_job(
            prepare_ligand_shards_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def run_shards(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
            hit_threshold: float,
            hit_cap: int,
            output_dir: PathLike | None = None,
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
            run_id: str | None = None,
            protocol_metadata: dict | None = None,
            engine_config: dict[str, Any] | None = None,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            skip_existing: bool = True,
            payload: str = PAYLOAD_LIGHT,
            check_required: bool = True,
            selected_from_receptor_id: int | None = None,
            selected_top_n: int | None = None,
    ) -> str:
        """`run` for a sharded library: the campaign, with the gate that makes it end.

        No ligand scope — the library is the inventory of prepared shards. When the run ends, the
        best `hit_cap` ligands per receptor that pass the `hit_threshold` prefilter become rows;
        both are required, not defaulted: a campaign without them writes back everything it
        scored, which is the failure mode `htpvs` exists to prevent. A dependent off-target
        stage sets `selected_from_receptor_id`; its deferred chunker reads that receptor's
        durable Top-N and extracts only those records from the source shards.
        """
        self.runtime._require_active_project()
        if selected_top_n is not None and selected_from_receptor_id is None:
            raise ValueError("selected_top_n requires selected_from_receptor_id.")
        if selected_from_receptor_id is not None and not str(run_id or "").strip():
            raise ValueError("Off-target selection requires the reference run_id.")
        if (box_center is None) ^ (box_size is None):
            raise ValueError("docking.run requires both box_center and box_size when either one is provided.")
        program_spec = self.get_program_spec(program)
        if not program_spec.supports_shards:
            raise ValueError(f"Docking program '{program_spec.key}' does not support sharded libraries.")
        knobs = dict(
            exhaustiveness=exhaustiveness,
            num_modes=num_modes,
            scoring_function=scoring_function,
            vina_backend=vina_backend,
            vina_command=vina_command,
            vina_cpu=vina_cpu,
            seed=seed,
            spacing=spacing,
            energy_range=energy_range,
            min_rmsd=min_rmsd,
        )
        resolved_engine_config = _validated_engine_config(program_spec, engine_config, **knobs)
        receptor_set_ref = self._set_ref(receptor_set, name="docking_receptor_input")
        receptor_scope = scope_payload(receptor_set) if is_molecule_scope(receptor_set) else {}
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        params = DockShardsJobParams(
            output_dir=self._output_dir(output_dir),
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            receptor_filters=receptor_filters,
            box_center=box_center,
            box_size=box_size,
            **_engine_fields(resolved_engine_config, knobs),
            run_id=str(run_id or uuid4().hex),
            protocol_metadata=DockingProtocolMetadata.from_mapping(protocol_metadata or {}),
            engine_config=resolved_engine_config,
            hit_threshold=float(hit_threshold),
            hit_cap=int(hit_cap),
            selected_from_receptor_id=selected_from_receptor_id,
            selected_top_n=(int(selected_top_n) if selected_top_n is not None else int(hit_cap)),
            skip_existing=skip_existing,
            check_required=check_required,
        )
        return self.runtime.submit_job(
            dock_shards_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            cpu_required=program_spec.resource_requirements(resolved_engine_config)["cpu_required"],
            # The gate lives in the parent, like the shard queue at import: it counts across
            # chunks and it writes molecules, neither of which a worker can do.
            result_handler_kwargs={
                "project_db": self.runtime.workspace.project_db,
                "hit_cap": int(hit_cap),
                "hit_threshold": float(hit_threshold),
                "run_id": params.run_id,
                "engine": params.engine,
                "protocol_hash": str(params.protocol_metadata.hash or ""),
                "payload": str(payload),
                "selected_from_receptor_id": params.selected_from_receptor_id,
            },
        )

    def prepare_receptors(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            receptors: MoleculeSetRef | MoleculeScope | Selection[MoleculeRecord] | int | None = None,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
            batch_size: int | None = None,  # None -> settings (batch_sizes.receptor)
            force: bool = False,
            keep_waters: bool = False,
            keep_cofactors: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
    ) -> str:
        if receptors is not None:
            if receptor_set is not None:
                raise ValueError("Pass either receptors or receptor_set, not both.")
            receptor_set = receptors
        program_spec = self.get_program_spec(program)
        if not program_spec.requires_receptor_preparation:
            raise ValueError(f"Docking program '{program_spec.key}' does not require receptor preparation.")
        receptor_set_ref = None if receptor_set is None or is_molecule_scope(receptor_set) else ensure_molecule_set_ref(
            self.runtime, receptor_set, name="prepare_receptors_input")
        receptor_scope = scope_payload(receptor_set) if is_molecule_scope(receptor_set) else {}
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        params = PreparationJobParams(
            batch_size=batch_size,
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            receptor_filters=receptor_filters,
            engine=program_spec.preparation_engine,
            force=force,
            keep_waters=keep_waters,
            keep_cofactors=keep_cofactors,
        )
        return self._submit_preparation_job(
            prepare_receptors_job,
            params=params,
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def set_grid(
            self,
            *,
            receptor_id: int,
            center: tuple[float, float, float],
            size: tuple[float, float, float],
            spacing: float = 0.375,
            engine: str = "vina",
            metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.runtime._require_active_project()
        engine_name = str(engine or "vina")
        site = self.runtime.binding_sites.save_site(
            molecule_id=int(receptor_id),
            name="Manual Site",
            source="manual",
            source_ref="",
            center=tuple(float(value) for value in center),
            size=tuple(float(value) for value in size),
            binding_site_id=None,
            set_active=True,
            extra_data={
                **dict(metadata or {}),
                "engine": engine_name,
                "spacing": float(spacing),
            },
        )
        return {
            "receptor_id": int(receptor_id),
            "engine": engine_name,
            "binding_site_id": int(site.id or 0) or None,
            "center": [float(site.center_x or 0.0), float(site.center_y or 0.0), float(site.center_z or 0.0)],
            "size": [float(site.size_x or 0.0), float(site.size_y or 0.0), float(site.size_z or 0.0)],
            "spacing": float((site.extra_data or {}).get("spacing") or spacing),
        }

    def get_grid(self, *, receptor_id: int, engine: str = "vina") -> dict[str, Any] | None:
        self.runtime._require_active_project()
        engine_name = str(engine or "vina")
        with self.runtime.workspace.project_db.get_session() as session:
            receptor = session.get(MoleculeRecord, int(receptor_id))
            if receptor is None or not bool(getattr(receptor, "is_receptor", False)):
                return None
            site = _active_site(session, receptor)
        if site is None or not bool(site.is_defined):
            return None
        extra_data = dict(site.extra_data or {})
        return {
            "engine": engine_name,
            "binding_site_id": int(site.id or 0) or None,
            "center": [float(site.center_x or 0.0), float(site.center_y or 0.0), float(site.center_z or 0.0)],
            "size": [float(site.size_x or 0.0), float(site.size_y or 0.0), float(site.size_z or 0.0)],
            "spacing": float(extra_data.get("spacing") or 0.375),
        }

    @contextlib.contextmanager
    def _receptor_as_pdb(self, receptor):
        """The receptor's stored structure as a PDB file for the block, or None when it has none.
        The source structure is read, not the prepared one: preparation keeps its metals."""
        payload_id = molecule_payload_id(receptor)
        if payload_id is None:
            yield None
            return
        text, kind = read_structure(self.runtime.workspace.project_db, payload_id)
        with scratch_file(text.encode(), f".{kind}") as path, as_pdb(path) as readable:
            yield readable

    def method_misfit_receptor_ids(self, *, method: str) -> list[int]:
        """Receptors whose active box does not fit the docking `method` (methods.py).
        Receptors without a box are not judged: the user still has to define one. The source
        structure is read: preparation keeps its metals."""
        from amdockvs.docking.methods import excludes

        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            rows = session.exec(
                select(MoleculeRecord, BindingSite)
                .join(BindingSite, (BindingSite.molecule_id == MoleculeRecord.id) & (BindingSite.is_active == True))  # noqa: E712
                .where(MoleculeRecord.is_receptor == True)  # noqa: E712 - SQL expression
            ).all()
        misfits: list[int] = []
        for receptor, site in rows:
            box = site_payload(site)
            if box is None:
                continue
            with self._receptor_as_pdb(receptor) as readable:  # mmCIF too: the Zn check reads PDB columns
                if readable is not None and excludes(method, readable, box["center"], box["size"]):
                    misfits.append(int(receptor.id))
        return misfits

    def check_required(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
    ) -> dict[str, Any]:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        ligand_set_ref = None if ligand_set is None or is_molecule_scope(ligand_set) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="docking_check_ligand_input")
        receptor_set_ref = None if receptor_set is None or is_molecule_scope(receptor_set) else ensure_molecule_set_ref(
            self.runtime, receptor_set, name="docking_check_receptor_input")
        ligand_scope = scope_payload(ligand_set) if is_molecule_scope(ligand_set) else {}
        receptor_scope = scope_payload(receptor_set) if is_molecule_scope(receptor_set) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        project_db = self.runtime.workspace.project_db
        # Ligands are counted, never listed: "prepared?" is a subquery, so a million-ligand
        # library costs two COUNTs and a sample. Receptors stay on list_entity_rows — there
        # are tens of them and the grid check needs the hydrated binding-site payload.
        ligand_scope = dict(
            entity_kind="ligand",
            engine=program_spec.preparation_engine,
            set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
        )
        ligands_total = count_entity_rows(project_db, **ligand_scope, filters=ligand_filters)
        unprepared_filters = {**ligand_filters, "prepared_engine": False}
        missing_ligands = (
            entity_ids(project_db, **ligand_scope, filters=unprepared_filters, limit=MISSING_SAMPLE_SIZE + 1)
            if program_spec.requires_ligand_preparation
            else []
        )
        ligands_missing = (
            len(missing_ligands)
            if len(missing_ligands) <= MISSING_SAMPLE_SIZE
            else count_entity_rows(project_db, **ligand_scope, filters=unprepared_filters)
        )
        receptor_rows = list_entity_rows(
            project_db,
            entity_kind="receptor",
            engine=program_spec.preparation_engine,
            set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            filters=receptor_filters,
            fields=("id", "prepared_engine", "grid_engine"),
            order=("id",),
        )
        receptor_ids = [int(row.get("id") or 0) for row in receptor_rows if int(row.get("id") or 0) > 0]
        ready_receptor_ids = {
            int(row.get("id") or 0)
            for row in receptor_rows
            if not program_spec.requires_receptor_preparation or bool(row.get("prepared_engine"))
        }
        ready_grid_receptor_ids = {
            int(row.get("id") or 0)
            for row in receptor_rows
            if not program_spec.requires_binding_site or bool(row.get("grid_engine"))
        }
        missing_receptors_prepared = [value for value in receptor_ids if value not in ready_receptor_ids]
        missing_receptor_grids = [value for value in receptor_ids if value not in ready_grid_receptor_ids]

        ready = (
                bool(ligands_total)
                and bool(receptor_ids)
                and not missing_ligands
                and not missing_receptors_prepared
                and not missing_receptor_grids
        )
        return {
            "ready": ready,
            "operation": program_spec.operation_name("run"),
            # Samples, not full lists: these end up in an error dialog.
            "missing": {
                "ligands_prepared": missing_ligands[:MISSING_SAMPLE_SIZE],
                "receptors_prepared": missing_receptors_prepared[:MISSING_SAMPLE_SIZE],
                "receptor_binding_sites": missing_receptor_grids[:MISSING_SAMPLE_SIZE],
            },
            "required_jobs": program_spec.required_jobs(),
            "counts": {
                "ligands_total": ligands_total,
                "ligands_ready": ligands_total - ligands_missing,
                "ligands_missing": ligands_missing,
                "receptors_total": len(receptor_rows),
                "receptors_ready": len(ready_receptor_ids),
                "receptors_missing": len(missing_receptors_prepared),
                "receptor_grids_ready": len(ready_grid_receptor_ids),
                "receptor_grids_missing": len(missing_receptor_grids),
            },
        }

    def list_box_residues(self, *, receptor_id: int, engine: str = "vina") -> list[dict[str, Any]]:
        """Residues with at least one atom inside the receptor's active binding-site box.

        Candidate set for flexible-residue selection — short because the box filters it.
        Returns [] when there's no active grid or the structure file is unavailable.
        """
        grid = self.get_grid(receptor_id=int(receptor_id), engine=engine)
        if grid is None:
            return []
        center = tuple(float(v) for v in grid.get("center") or [])
        size = tuple(float(v) for v in grid.get("size") or [])
        if len(center) != 3 or len(size) != 3:
            return []
        with self.runtime.workspace.project_db.get_session() as session:
            receptor = session.get(MoleculeRecord, int(receptor_id))
        if receptor is None:
            return []
        try:
            with self._receptor_as_pdb(receptor) as readable_receptor:
                if readable_receptor is None:
                    return []
                residues = residues_in_box(
                    readable_receptor.read_text(encoding="utf-8", errors="ignore"), center, size
                )
        except (OSError, ValueError):
            return []
        return [
            {"key": r.key, "label": r.label, "chain": r.chain, "resname": r.resname, "resnum": r.resnum}
            for r in residues
        ]

    def get_flexible_residues(self, *, receptor_id: int) -> list[str]:
        # Flexible residues belong to the receptor (what gets passed to prep), not to any
        # binding site — the box is only a candidate filter, not the owner of the selection.
        with self.runtime.workspace.project_db.get_session() as session:
            prep = self._ad4_receptor_prep(session, receptor_id)
            return [str(k) for k in (prep.flex_residues if prep else [])]

    @staticmethod
    def _ad4_receptor_prep(session, receptor_id: int):
        # ponytail: flexible side chains are an ad4 (Vina family) concept; key by family if another engine docks flexibly.
        return session.exec(select(ReceptorPrep).where(
            ReceptorPrep.receptor_molecule_id == int(receptor_id), ReceptorPrep.family == "ad4")).first()

    def set_flexible_residues(self, *, receptor_id: int, residue_keys: list[str]) -> list[str]:
        keys = [str(k) for k in residue_keys]
        with self.runtime.workspace.project_db.get_session() as session:
            if session.get(MoleculeRecord, receptor_id) is None:
                raise ValueError(f"No receptor with id {receptor_id}.")
            prep = self._ad4_receptor_prep(session, receptor_id) or ReceptorPrep(receptor_molecule_id=receptor_id)
            prep.flex_residues = keys
            session.add(prep)
            session.commit()
        return keys

    def check_receptors(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
    ) -> dict[str, Any]:
        """Receptor-only half of check_required: preparation + grid status, no ligand scan.

        check_required materializes the full ligand set to report missing ligands; that's
        the expensive part. The Receptors step only shows receptor prep/grid status, so this
        skips ligands entirely (receptor sets are small).
        """
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        receptor_set_ref = (
            None
            if receptor_set is None or is_molecule_scope(receptor_set)
            else ensure_molecule_set_ref(self.runtime, receptor_set, name="docking_check_receptor_input")
        )
        receptor_scope = scope_payload(receptor_set) if is_molecule_scope(receptor_set) else {}
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        receptor_rows = list_entity_rows(
            self.runtime.workspace.project_db,
            entity_kind="receptor",
            engine=program_spec.preparation_engine,
            set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            filters=receptor_filters,
            fields=("id", "prepared_engine", "grid_engine"),
            order=("id",),
        )
        receptor_ids = [int(row.get("id") or 0) for row in receptor_rows if int(row.get("id") or 0) > 0]
        ready_prepared = {
            int(row.get("id") or 0)
            for row in receptor_rows
            if not program_spec.requires_receptor_preparation or bool(row.get("prepared_engine"))
        }
        ready_grid = {
            int(row.get("id") or 0)
            for row in receptor_rows
            if not program_spec.requires_binding_site or bool(row.get("grid_engine"))
        }
        return {
            "receptor_ids": receptor_ids,
            "missing": {
                "receptors_prepared": [rid for rid in receptor_ids if rid not in ready_prepared],
                "receptor_binding_sites": [rid for rid in receptor_ids if rid not in ready_grid],
            },
            "counts": {
                "receptors_total": len(receptor_rows),
                "receptors_ready": len(ready_prepared),
                "receptor_grids_ready": len(ready_grid),
            },
        }

    def _grid_from_receptor_source(
            self,
            receptor_source,
            *,
            engine: str,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float], float] | None:
        if receptor_source is None:
            return None
        if is_molecule_scope(receptor_source):
            receptor_ids = list(islice(self.runtime.molecules.stream_ids(receptor_source), 2))
        else:
            receptor_ids = list_receptor_ids_in_set(
                self.runtime.workspace.project_db,
                receptor_set_id=int(receptor_source.id),
            )
        if len(receptor_ids) != 1:
            return None
        grid = self.get_grid(receptor_id=receptor_ids[0], engine=engine)
        if not grid:
            return None
        center = tuple(float(value) for value in (grid.get("center") or []))
        size = tuple(float(value) for value in (grid.get("size") or []))
        if len(center) != 3 or len(size) != 3:
            return None
        return center, size, float(grid.get("spacing") or 0.375)

    def _output_dir(self, output_dir: PathLike | None) -> str:
        if output_dir is not None:
            return str(Path(output_dir).expanduser().resolve())
        return str(self.runtime.get_project_resource_path("docking_results"))

    def _set_ref(self, value, *, name: str) -> MoleculeSetRef | None:
        """A stored set for anything but a scope: a scope is applied as filters, not stored."""
        if value is None or is_molecule_scope(value):
            return None
        return ensure_molecule_set_ref(self.runtime, value, name=name)

    def run(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligands: MoleculeSetRef | MoleculeScope | Selection[MoleculeRecord] | int | None = None,
            receptors: MoleculeSetRef | MoleculeScope | Selection[MoleculeRecord] | int | None = None,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
            output_dir: PathLike | None = None,
            batch_size: int | None = None,
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
            run_id: str | None = None,
            protocol_metadata: dict | None = None,
            engine_config: dict[str, Any] | None = None,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
            skip_existing: bool = True,
            compute_interactions: bool = False,
            hit_threshold: float | None = None,
            hit_cap: int = 0,
            payload: str = PAYLOAD_LIGHT,
            selected_from_receptor_id: int | None = None,
            selected_top_n: int | None = None,
            selected_max_score: float | None = None,
    ) -> str:
        """Run docking over the active library, selecting its row or shard adapter.

        ``hit_threshold``/``hit_cap`` are optional for materialized rows and mandatory for a
        sharded library, where returning every scored molecule would defeat the storage model.
        ``compute_interactions`` finds each pose's interactions in the chunk that docked it.
        An off-target stage sets ``selected_from_receptor_id``/``selected_top_n``: the other
        receptors dock only that receptor's Top-N ligands, read when the job starts — so it
        `depends_on` the target's job. Over rows, ``selected_max_score`` keeps only those at
        or below it (a sharded library uses ``hit_threshold`` for that).
        """
        if selected_top_n is not None and selected_from_receptor_id is None:
            raise ValueError("selected_top_n requires selected_from_receptor_id.")
        if ligands is not None:
            if ligand_set is not None:
                raise ValueError("Pass either ligands or ligand_set, not both.")
            ligand_set = ligands
        if receptors is not None:
            if receptor_set is not None:
                raise ValueError("Pass either receptors or receptor_set, not both.")
            receptor_set = receptors
        self.runtime._require_active_project()
        knobs = dict(
            exhaustiveness=exhaustiveness,
            num_modes=num_modes,
            scoring_function=scoring_function,
            vina_backend=vina_backend,
            vina_command=vina_command,
            vina_cpu=vina_cpu,
            seed=seed,
            spacing=spacing,
            energy_range=energy_range,
            min_rmsd=min_rmsd,
        )
        if ligand_set is None and has_shards(self.runtime.workspace.project_db):
            if hit_threshold is None or int(hit_cap) <= 0:
                raise ValueError(
                    "docking.run over a sharded library requires hit_threshold and hit_cap."
                )
            if compute_interactions:
                raise ValueError(
                    "compute_interactions is not available during sharded docking; compute "
                    "interactions after hits have been materialized."
                )
            return self.run_shards(
                program=program,
                receptor_set=receptor_set,
                hit_threshold=float(hit_threshold),
                hit_cap=int(hit_cap),
                output_dir=output_dir,
                box_center=box_center,
                box_size=box_size,
                **knobs,
                run_id=run_id,
                protocol_metadata=protocol_metadata,
                engine_config=engine_config,
                executor_name=executor_name,
                depends_on=depends_on,
                skip_existing=skip_existing,
                payload=payload,
                check_required=check_required,
                selected_from_receptor_id=selected_from_receptor_id,
                selected_top_n=selected_top_n,
            )
        if selected_from_receptor_id is not None and not selected_top_n:
            raise ValueError("An off-target stage over ligand rows requires selected_top_n.")
        if hit_cap and hit_threshold is None:
            raise ValueError(
                "docking.run needs hit_threshold when hit_cap is set: the threshold is the "
                "scientific criterion, the cap is only what makes the run end."
            )
        program_spec = self.get_program_spec(program)
        resolved_engine_config = _validated_engine_config(program_spec, engine_config, **knobs)
        resolved_batch_size = max(1, int(batch_size or app_config(self.runtime).docking.batch_size))
        ligand_set_ref = self._set_ref(ligand_set, name="docking_ligand_input")
        receptor_set_ref = self._set_ref(receptor_set, name="docking_receptor_input")
        ligand_scope = scope_payload(ligand_set) if is_molecule_scope(ligand_set) else {}
        receptor_scope = scope_payload(receptor_set) if is_molecule_scope(receptor_set) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        if box_center is None or box_size is None:
            resolved_grid = self._grid_from_receptor_source(
                receptor_set if is_molecule_scope(receptor_set) else receptor_set_ref,
                engine=program_spec.preparation_engine,
            )
            if resolved_grid is not None:
                box_center, box_size, knobs["spacing"] = resolved_grid
        if (box_center is None) ^ (box_size is None):
            raise ValueError("docking.run requires both box_center and box_size when either one is provided.")
        # Always resolve counts: they gate readiness (when requested) AND give the true
        # chunk total so progress is computed against the real pair count instead of
        # completed/emitted-so-far (a lazy generator otherwise leaves total_chunks unknown,
        # making progress hover near 50-60% regardless of actual completion).
        requirement_check = self.check_required(
            program=program_spec.key,
            ligand_set=ligand_set if is_molecule_scope(ligand_set) else ligand_set_ref,
            receptor_set=receptor_set if is_molecule_scope(receptor_set) else receptor_set_ref,
        )
        if check_required and not bool(requirement_check.get("ready")):
            raise RuntimeError(_requirements_message(program_spec.key, requirement_check))
        run_counts = dict(requirement_check.get("counts") or {})
        # The docking feed pairs every prepared ligand with every prepared receptor
        # (receptors without a grid still emit a failed-pair chunk), batched by batch_size.
        total_pairs = int(run_counts.get("ligands_ready") or 0) * int(run_counts.get("receptors_ready") or 0)
        if check_required and total_pairs <= 0:
            # Fail early and clearly instead of submitting a job whose feed yields no pairs
            # (which would otherwise complete silently with zero results).
            raise RuntimeError(
                f"docking.run has no valid ligand-receptor pairs to dock for docking.{program_spec.key}: "
                f"counts={run_counts}. Import and prepare at least one ligand and one receptor first."
            )
        # The feed applies skip_existing after resolving the scoped cross-product. Its declared
        # total must use that same filtered count; over-declaring makes Molsuite wait forever for
        # chunks that the feed intentionally never emits.
        pending_pairs = count_pending_docking_pairs(
            project_db=self.runtime.workspace.project_db,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            ligand_filters=ligand_filters,
            receptor_filters=receptor_filters,
            protocol_metadata=protocol_metadata,
            skip_existing=skip_existing,
            requires_ligand_preparation=program_spec.requires_ligand_preparation,
            requires_receptor_preparation=program_spec.requires_receptor_preparation,
        )
        # The Top-N does not exist yet when an off-target stage is submitted: declare nothing
        # (a floor of 0) rather than a count that could over-declare and never complete.
        total_chunks = (
            -(-pending_pairs // resolved_batch_size)
            if pending_pairs > 0 and selected_from_receptor_id is None else 0
        )
        params = DockingJobParams(
            output_dir=self._output_dir(output_dir),
            batch_size=resolved_batch_size,
            row_batch_size=batch_size_for("ligand", self.runtime),
            ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            ligand_filters=ligand_filters,
            receptor_filters=receptor_filters,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            requires_ligand_preparation=program_spec.requires_ligand_preparation,
            requires_receptor_preparation=program_spec.requires_receptor_preparation,
            requires_binding_site=program_spec.requires_binding_site,
            box_center=box_center,
            box_size=box_size,
            **_engine_fields(resolved_engine_config, knobs),
            run_id=str(run_id or uuid4().hex),
            protocol_metadata=dict(protocol_metadata or {}),
            engine_config=resolved_engine_config,
            skip_existing=skip_existing,
            hit_threshold=hit_threshold,
            hit_cap=max(0, int(hit_cap)),
            interactions=bool(compute_interactions),
            selected_from_receptor_id=selected_from_receptor_id,
            selected_top_n=selected_top_n,
            selected_max_score=selected_max_score,
        )
        job_id = self.runtime.submit_job(
            docking_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            # Each docking chunk runs one engine process that uses `vina_cpu` threads
            # internally, so reserve that many CPU tokens from the pool to avoid
            # oversubscription (pool limits concurrency to floor(cpus / vina_cpu)).
            cpu_required=program_spec.resource_requirements(resolved_engine_config)["cpu_required"],
            # Declare the real chunk total so progress = processed/total (not processed/emitted).
            total_chunks=total_chunks,
        )
        return job_id

    def redock(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            complex_set: ComplexSetRef | int | None = None,
            complex_ids: list[int] | None = None,
            purpose: str = "redocking,reference",
            output_dir: PathLike | None = None,
            batch_size: int = DEFAULT_DOCKING_BATCH_SIZE,
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
            run_id: str | None = None,
            protocol_metadata: dict | None = None,
            engine_config: dict[str, Any] | None = None,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
            skip_existing: bool = True,
    ) -> str:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        knobs = dict(
            exhaustiveness=exhaustiveness,
            num_modes=num_modes,
            scoring_function=scoring_function,
            vina_backend=vina_backend,
            vina_command=vina_command,
            vina_cpu=vina_cpu,
            seed=seed,
            spacing=spacing,
            energy_range=energy_range,
            min_rmsd=min_rmsd,
        )
        resolved_engine_config = _validated_engine_config(program_spec, engine_config, **knobs)
        complex_set_id = None if complex_set is None else int(
            complex_set.id if isinstance(complex_set, ComplexSetRef) else complex_set)
        explicit_complex_ids = sorted({int(value) for value in (complex_ids or []) if int(value) > 0})
        purpose_text = str(purpose or "redocking").strip() or "redocking"
        if check_required:
            complex_rows = list_complex_rows(
                self.runtime.workspace.project_db,
                set_id=complex_set_id,
                purpose=purpose_text,
            )
            if explicit_complex_ids:
                allowed = set(explicit_complex_ids)
                complex_rows = [row for row in complex_rows if int(row.get("id") or 0) in allowed]
            if not complex_rows:
                raise RuntimeError("docking.redock did not resolve any complexes to process.")
            molecule_ids: list[int] = []
            for row in complex_rows:
                receptor_id = int(row.get("receptor_molecule_id") or 0)
                ligand_id = int(row.get("ligand_molecule_id") or 0)
                if receptor_id > 0:
                    molecule_ids.append(receptor_id)
                if ligand_id > 0:
                    molecule_ids.append(ligand_id)
            molecule_rows = get_molecule_rows_by_ids(
                self.runtime.workspace.project_db,
                molecule_ids,
                engine=program_spec.preparation_engine,
            )
            missing = {
                "ligands_prepared": [],
                "receptors_prepared": [],
                "receptor_binding_sites": [],
            }
            for row in complex_rows:
                receptor_id = int(row.get("receptor_molecule_id") or 0)
                ligand_id = int(row.get("ligand_molecule_id") or 0)
                receptor_row = molecule_rows.get(receptor_id) or {}
                ligand_row = molecule_rows.get(ligand_id) or {}
                if program_spec.requires_ligand_preparation and not bool(ligand_row.get("prepared_engine")):
                    missing["ligands_prepared"].append(ligand_id)
                if program_spec.requires_receptor_preparation and not bool(receptor_row.get("prepared_engine")):
                    missing["receptors_prepared"].append(receptor_id)
                if (
                    program_spec.requires_binding_site
                    and (box_center is None or box_size is None)
                    and not bool(receptor_row.get("grid_engine"))
                ):
                    missing["receptor_binding_sites"].append(receptor_id)
            missing = {
                key: sorted({int(value) for value in values if int(value) > 0})
                for key, values in missing.items()
            }
            if any(missing.values()):
                required_jobs = program_spec.required_jobs()
                raise RuntimeError(
                    f"docking.redock requirements are not satisfied for docking.{program_spec.key}. "
                    f"missing={missing}; required_jobs={required_jobs}"
                )
        pending_pairs = count_pending_redocking_pairs(
            project_db=self.runtime.workspace.project_db,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            complex_set_id=complex_set_id,
            complex_ids=explicit_complex_ids,
            purpose=purpose_text,
            protocol_metadata=protocol_metadata,
            skip_existing=skip_existing,
        )
        total_chunks = (
            -(-pending_pairs // max(1, int(batch_size))) if pending_pairs > 0 else 0
        )
        params = RedockingJobParams(
            output_dir=self._output_dir(output_dir),
            batch_size=batch_size,
            complex_set_id=complex_set_id,
            complex_ids=explicit_complex_ids,
            purpose=purpose_text,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            requires_binding_site=program_spec.requires_binding_site,
            box_center=box_center,
            box_size=box_size,
            **_engine_fields(resolved_engine_config, knobs),
            run_id=str(run_id or uuid4().hex),
            protocol_metadata=dict(protocol_metadata or {}),
            engine_config=resolved_engine_config,
            skip_existing=skip_existing,
        )
        job_id = self.runtime.submit_job(
            redocking_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            cpu_required=program_spec.resource_requirements(resolved_engine_config)["cpu_required"],
            total_chunks=total_chunks,
        )
        return job_id

    def list_results(self, *, limit: int = 5000, offset: int = 0):
        return results_repo.list_results(self._project_db(), limit=limit, offset=offset)

    def result_stats(self):
        return results_repo.get_docking_results_stats(self._project_db())

    def top_hits(self, *, limit: int = 25, receptor_id: int | None = None, only_completed: bool = True):
        return results_repo.list_top_hits(
            self._project_db(),
            limit=limit,
            receptor_id=receptor_id,
            only_completed=only_completed,
        )

    def ligand_summaries(
            self,
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
            exclude_run_kind: str | None = "redocking",
    ):
        """(best pose, pose count) per ligand, one page at a time."""
        return results_repo.list_ligand_result_summaries(
            self._project_db(),
            limit=limit,
            offset=offset,
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

    def filtered_hits(
            self,
            *,
            limit: int = 5000,
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
            offset: int = 0,
    ):
        return results_repo.list_top_hits(
            self._project_db(),
            limit=limit,
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
            offset=offset,
        )

    def hit(self, *, result_id: int):
        return results_repo.get_hit(self._project_db(), result_id=int(result_id))

    def result_protocols(self, *, receptor_id: int | None = None, exclude_run_kind: str | None = "redocking"):
        return results_repo.list_result_protocols(
            self._project_db(),
            receptor_id=receptor_id,
            exclude_run_kind=exclude_run_kind,
        )

    def compute_interactions(
            self,
            *,
            result_ids: list[int] | None = None,
            run_id: str = "",
            receptor_id: int | None = None,
            score_lte: float | None = None,
            pose_rank: int | None = 1,
            method: str = "auto",
            chunk_size: int = 128,
            replace_existing: bool = True,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
    ) -> str:
        self.runtime._require_active_project()
        params = InteractionJobParams(
            result_ids=[int(value) for value in (result_ids or []) if int(value) > 0],
            run_id=str(run_id or ""),
            receptor_id=receptor_id,
            score_lte=score_lte,
            pose_rank=pose_rank,
            method=str(method or "geometry"),
            chunk_size=max(1, int(chunk_size)),
            replace_existing=bool(replace_existing),
        )
        return self.runtime.submit_job(
            interactions_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def compute_diagrams(
            self,
            *,
            result_ids: list[int] | None = None,
            run_id: str = "",
            receptor_id: int | None = None,
            score_lte: float | None = None,
            pose_rank: int | None = 1,
            fmt: str = "png",
            chunk_size: int = 64,
            replace_existing: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
    ) -> str:
        """Render 2D interaction diagrams for docking poses as a standalone job.

        Files land next to each pose (see ``diagram.diagram_path_for``); nothing
        is written to the DB, so re-run with ``replace_existing=True`` to refresh.
        """
        self.runtime._require_active_project()
        params = DiagramJobParams(
            result_ids=[int(value) for value in (result_ids or []) if int(value) > 0],
            run_id=str(run_id or ""),
            receptor_id=receptor_id,
            score_lte=score_lte,
            pose_rank=pose_rank,
            fmt=str(fmt or "png"),
            chunk_size=max(1, int(chunk_size)),
            replace_existing=bool(replace_existing),
        )
        return self.runtime.submit_job(
            diagram_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def diagram_path(self, *, pose_path: str, pose_rank: int = 1, fmt: str = "png") -> str:
        """Convention path for a pose's diagram (may not exist yet)."""
        from amdockvs.docking.results.diagram import diagram_path_for

        suffix = ".svg" if str(fmt).lower() == "svg" else ".png"
        return str(diagram_path_for(pose_path, pose_rank, suffix=suffix))

    def list_interactions(self, *, result_id: int):
        return results_repo.list_interactions(self._project_db(), result_id=int(result_id))

    def interaction_stats(self, *, result_ids: list[int] | None = None):
        return results_repo.interaction_stats(self._project_db(), result_ids=result_ids)

    def count_docked_pairs(
            self,
            *,
            engine: str = "",
            receptor_ids: list[int] | None = None,
            protocol_hash: str | None = None,
            run_kind: str | None = "screening",
    ) -> int:
        return results_repo.count_docked_pairs(
            self._project_db(),
            engine=engine,
            receptor_ids=receptor_ids,
            protocol_hash=protocol_hash,
            run_kind=run_kind,
        )

    def receptor_summaries(self):
        return results_repo.list_receptor_result_summaries(self._project_db())

    def pivot_availability(self) -> dict[str, bool]:
        return results_repo.pivot_availability(self._project_db())

    def offtarget_rows(
            self,
            *,
            receptor_ids: list[int] | None = None,
            ligand_ids: list[int] | None = None,
            protocol_hash: str | None = None,
            limit: int = 50000,
    ):
        return results_repo.list_offtarget_rows(
            self._project_db(),
            receptor_ids=receptor_ids, ligand_ids=ligand_ids, protocol_hash=protocol_hash, limit=limit
        )

    def max_pose_rank(self, *, receptor_ids: list[int]) -> int:
        return results_repo.max_pose_rank(self._project_db(), receptor_ids=receptor_ids)


__all__ = ["DockingAPI"]
