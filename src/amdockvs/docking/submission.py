"""Docking plan materialization and submission through the public runtime API."""
from __future__ import annotations

from dataclasses import dataclass

from amdockvs.docking.planning import DockingRunRequest, protocol_job_key
from amdockvs.docking.engines.programs import get_docking_program


@dataclass(frozen=True)
class DockingSubmission:
    job_ids: dict[str, str]
    interaction_job_id: str = ""
    receptor_ids: tuple[int, ...] = ()
    complex_ids: tuple[int, ...] = ()


class DockingSubmissionService:
    """Submit an immutable, already-validated request using ``runtime.docking`` only."""

    def __init__(self, runtime):
        self.runtime = runtime

    def submit(
        self,
        request: DockingRunRequest,
        *,
        ready_receptor_ids: tuple[int, ...] = (),
    ) -> DockingSubmission:
        if not request.protocols:
            raise ValueError("Select at least one compatible docking software first.")
        job_ids: dict[str, str] = {}
        if request.run_kind == "redocking":
            if not request.complex_ids:
                raise ValueError("No prepared original receptor-ligand pairs are ready for redocking.")
            for index, protocol in enumerate(request.protocols):
                config = dict(protocol.config)
                job_ids[protocol_job_key(protocol, index)] = self.runtime.docking.redock(
                    program=protocol.program,
                    complex_ids=list(request.complex_ids),
                    purpose="redocking,reference",
                    batch_size=request.batch_size,
                    exhaustiveness=int(config.get("exhaustiveness") or 8),
                    num_modes=int(config.get("num_modes") or 9),
                    scoring_function=str(config.get("scoring_function") or "vina"),
                    vina_backend=str(config.get("vina_backend") or "binary"),
                    vina_cpu=int(config.get("vina_cpu") or 1),
                    executor_name=request.executor_name,
                    skip_existing=request.skip_existing,
                    run_id=request.run_id,
                    protocol_metadata=protocol.to_mapping(),
                    engine_config=config,
                )
        else:
            if request.ligand_scope is None or request.receptor_scope is None:
                raise ValueError("Docking requires ligand and receptor scopes.")
            for index, protocol in enumerate(request.protocols):
                config = dict(protocol.config)
                program = get_docking_program(protocol.program)
                ligand_scope = (
                    self.runtime.molecules.filter(
                        request.ligand_scope,
                        filters={"prepared": True, "prepared_engine_key": program.preparation_engine},
                    )
                    if program.requires_ligand_preparation
                    else request.ligand_scope
                )
                common = dict(
                    program=protocol.program,
                    ligand_set=ligand_scope,
                    batch_size=request.batch_size,
                    exhaustiveness=int(config.get("exhaustiveness") or 8),
                    num_modes=int(config.get("num_modes") or 9),
                    scoring_function=str(config.get("scoring_function") or "vina"),
                    vina_backend=str(config.get("vina_backend") or "binary"),
                    vina_cpu=int(config.get("vina_cpu") or 1),
                    executor_name=request.executor_name,
                    skip_existing=request.skip_existing,
                    run_id=request.run_id,
                    protocol_metadata=protocol.to_mapping(),
                    engine_config=config,
                    compute_interactions=request.compute_interactions,
                )
                key = protocol_job_key(protocol, index)
                reference_id = request.offtarget_reference_id
                if reference_id is None or len(ready_receptor_ids) < 2:
                    job_ids[key] = self.runtime.docking.run(receptor_set=request.receptor_scope, **common)
                    continue
                if not int(request.offtarget_top_n):
                    raise ValueError("Off-target docking needs a Top-N greater than zero.")
                reference_job = self.runtime.docking.run(
                    receptor_set=self.runtime.molecules.filter(
                        request.receptor_scope, filters={"id": int(reference_id)}
                    ),
                    **common,
                )
                job_ids[f"{key}:reference"] = reference_job
                # The feed drops the reference receptor and reads its Top-N when the job starts.
                job_ids[f"{key}:off-target"] = self.runtime.docking.run(
                    receptor_set=request.receptor_scope,
                    depends_on=[reference_job],
                    selected_from_receptor_id=int(reference_id),
                    selected_top_n=int(request.offtarget_top_n),
                    selected_max_score=request.offtarget_max_score,
                    **common,
                )
        interaction_job_id = ""
        # A docking run finds them in its own chunks; a redock's poses get the job afterwards.
        if request.compute_interactions and request.run_kind == "redocking" and job_ids:
            interaction_job_id = self.runtime.docking.compute_interactions(
                run_id=request.run_id,
                pose_rank=None,
                executor_name=request.executor_name,
                depends_on=list(job_ids.values()),
            )
        return DockingSubmission(
            job_ids=job_ids,
            interaction_job_id=interaction_job_id,
            receptor_ids=tuple(ready_receptor_ids),
            complex_ids=tuple(request.complex_ids),
        )


__all__ = ["DockingSubmission", "DockingSubmissionService"]
