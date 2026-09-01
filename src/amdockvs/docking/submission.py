"""Docking plan materialization and submission through the public runtime API."""
from __future__ import annotations

from dataclasses import dataclass

from amdockvs.docking.planning import DockingRunRequest, protocol_job_key
from amdockvs.docking.programs import get_docking_program


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
                    compute_diagram=request.compute_diagram,
                )
        else:
            if request.ligand_scope is None or request.receptor_scope is None:
                raise ValueError("Docking requires ligand and receptor scopes.")
            for index, protocol in enumerate(request.protocols):
                config = dict(protocol.config)
                prep_engine = str(get_docking_program(protocol.program).preparation_engine)
                ligand_scope = self.runtime.molecules.filter(
                    request.ligand_scope,
                    filters={"prepared": True, "prepared_engine_key": prep_engine},
                )
                job_ids[protocol_job_key(protocol, index)] = self.runtime.docking.run(
                    program=protocol.program,
                    ligand_set=ligand_scope,
                    receptor_set=request.receptor_scope,
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
                    compute_diagram=request.compute_diagram,
                )
        interaction_job_id = ""
        if request.compute_interactions and job_ids:
            interaction_job_id = self.runtime.docking.compute_interactions(
                run_id=request.run_id,
                pose_rank=1,
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
