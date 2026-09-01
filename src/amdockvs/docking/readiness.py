"""Non-visual readiness evaluation for docking and redocking."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from amdockvs.api_common import MoleculeScope
from amdockvs.docking.planning import DockingProtocol
from amdockvs.docking.programs import get_docking_program
from amdockvs.vocab import MoleculeType


ACTIVE_JOB_STATUSES = ("pending", "running", "staging", "cancel_requested")


@dataclass(frozen=True)
class EntityReadiness:
    total: int = 0
    ready: int = 0
    failed: int = 0
    needs_selection: bool = False
    ready_ids: tuple[int, ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        payload = {
            "needs_selection": self.needs_selection,
            "total": self.total,
            "ready": self.ready,
            "failed": self.failed,
        }
        if self.ready_ids or not self.needs_selection:
            payload["ready_ids"] = list(self.ready_ids)
        return payload


@dataclass(frozen=True)
class PairReadiness:
    total: int = 0
    already: int = 0
    to_run: int = 0

    def as_mapping(self) -> dict[str, int]:
        return {"total": self.total, "already": self.already, "to_run": self.to_run}


@dataclass(frozen=True)
class DockingReadiness:
    mode: str
    ligands: EntityReadiness
    receptors: EntityReadiness
    pairs: PairReadiness = field(default_factory=PairReadiness)
    ready_complex_ids: tuple[int, ...] = ()
    total_complexes: int = 0

    @property
    def ready(self) -> bool:
        if self.mode == "redocking":
            return bool(self.ready_complex_ids)
        return (
            not self.ligands.needs_selection
            and self.ligands.ready > 0
            and not self.receptors.needs_selection
            and bool(self.receptors.ready_ids)
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ligands": self.ligands.as_mapping(),
            "receptors": self.receptors.as_mapping(),
            "ready_receptor_ids": list(self.receptors.ready_ids),
            "ready_complex_ids": list(self.ready_complex_ids),
            "total_complexes": self.total_complexes,
            "pairs": self.pairs.as_mapping(),
            "ready": self.ready,
        }


class DockingReadinessService:
    """Resolve preparation, grids, failures and already-docked pairs via runtime APIs."""

    def __init__(self, runtime):
        self.runtime = runtime

    def preparation(self, scope: MoleculeScope, *, role_type: str, engine: str) -> EntityReadiness:
        summary = self.runtime.docking.preparation_summary(
            scope,
            role_type=role_type,
            engine=engine,
        )
        return EntityReadiness(
            total=int(summary.get("total") or 0),
            ready=int(summary.get("ready") or 0),
            failed=int(summary.get("failed") or 0),
        )

    def receptors(self, scope: MoleculeScope, *, program: str) -> EntityReadiness:
        status = self.runtime.docking.check_receptors(program=program, receptor_set=scope)
        counts = dict(status.get("counts") or {})
        receptor_ids = tuple(int(value) for value in (status.get("receptor_ids") or ()) if int(value) > 0)
        missing = dict(status.get("missing") or {})
        no_prep = {int(value) for value in (missing.get("receptors_prepared") or ())}
        no_grid = {int(value) for value in (missing.get("receptor_binding_sites") or ())}
        ready_ids = tuple(value for value in receptor_ids if value not in no_prep and value not in no_grid)
        prep = self.preparation(
            scope,
            role_type="receptor",
            engine=str(get_docking_program(program).preparation_engine),
        )
        return EntityReadiness(
            total=int(counts.get("receptors_total") or 0),
            ready=int(counts.get("receptors_ready") or 0),
            failed=prep.failed,
            ready_ids=ready_ids,
        )

    def pairs(
        self,
        *,
        ligand_count: int,
        receptor_ids: Iterable[int],
        protocols: Iterable[DockingProtocol],
        skip_existing: bool,
    ) -> PairReadiness:
        receptors = tuple(sorted({int(value) for value in receptor_ids if int(value) > 0}))
        selected_protocols = tuple(protocols)
        total = max(0, int(ligand_count)) * len(receptors) * max(1, len(selected_protocols))
        already = 0
        if receptors and total:
            for protocol in selected_protocols:
                engine = str(get_docking_program(protocol.program).docking_engine)
                already += self.runtime.docking.count_docked_pairs(
                    engine=engine,
                    receptor_ids=list(receptors),
                    protocol_hash=protocol.hash or None,
                )
        already = min(already, total)
        return PairReadiness(
            total=total,
            already=already,
            to_run=max(0, total - already) if skip_existing else total,
        )

    def evaluate(
        self,
        *,
        ligand_scope: MoleculeScope | None,
        receptor_scope: MoleculeScope | None,
        protocols: Iterable[DockingProtocol],
        program: str,
        skip_existing: bool,
        ligand_needs_selection: bool = False,
        receptor_needs_selection: bool = False,
    ) -> DockingReadiness:
        selected_protocols = tuple(protocols)
        prep_engine = str(get_docking_program(program).preparation_engine)
        ligands = (
            EntityReadiness(needs_selection=True)
            if ligand_needs_selection or ligand_scope is None
            else self.preparation(ligand_scope, role_type="ligand", engine=prep_engine)
        )
        receptors = (
            EntityReadiness(needs_selection=True)
            if receptor_needs_selection or receptor_scope is None
            else self.receptors(receptor_scope, program=program)
        )
        pairs = self.pairs(
            ligand_count=0 if ligands.needs_selection else ligands.ready,
            receptor_ids=() if receptors.needs_selection else receptors.ready_ids,
            protocols=selected_protocols,
            skip_existing=skip_existing,
        )
        return DockingReadiness(
            mode="docking",
            ligands=ligands,
            receptors=receptors,
            pairs=pairs,
        )

    def evaluate_redocking(
        self,
        *,
        program: str,
        ligand_type: str = MoleculeType.SMALL_MOLECULE,
        receptor_type: str = MoleculeType.PROTEIN,
    ) -> DockingReadiness:
        complexes = tuple(self.runtime.complexes.list(purpose="redocking,reference"))
        ligand_ids = sorted({int(getattr(row, "ligand_molecule_id", 0) or 0) for row in complexes})
        receptor_ids = sorted({int(getattr(row, "receptor_molecule_id", 0) or 0) for row in complexes})
        prep_engine = str(get_docking_program(program).preparation_engine)
        ligand_scope = self.runtime.molecules.filter(
            self.runtime.molecules.all(),
            filters={
                "id__in": ligand_ids or [0],
                "is_ligand": True,
                "molecule_type": ligand_type,
                "excluded": False,
            },
        )
        prepared_ligand_scope = self.runtime.molecules.filter(
            ligand_scope,
            filters={"prepared": True, "prepared_engine_key": prep_engine},
        )
        prepared_ligand_ids = {
            int(value)
            for value in self.runtime.molecules.stream_ids(prepared_ligand_scope)
            if int(value) > 0
        }
        receptor_scope = self.runtime.molecules.filter(
            self.runtime.molecules.all(),
            filters={
                "id__in": receptor_ids or [0],
                "is_receptor": True,
                "molecule_type": receptor_type,
                "excluded": False,
            },
        )
        receptor_status = self.receptors(receptor_scope, program=program)
        ready_receptors = set(receptor_status.ready_ids)
        ready_complex_ids = tuple(
            int(getattr(row, "id", 0) or 0)
            for row in complexes
            if int(getattr(row, "id", 0) or 0) > 0
            and int(getattr(row, "ligand_molecule_id", 0) or 0) in prepared_ligand_ids
            and int(getattr(row, "receptor_molecule_id", 0) or 0) in ready_receptors
        )
        ligand_prep = self.preparation(ligand_scope, role_type="ligand", engine=prep_engine)
        ligands = EntityReadiness(
            total=len(ligand_ids),
            ready=len(prepared_ligand_ids),
            failed=ligand_prep.failed,
        )
        receptors = EntityReadiness(
            total=len(complexes),
            ready=len(ready_complex_ids),
            failed=receptor_status.failed,
            ready_ids=receptor_status.ready_ids,
        )
        return DockingReadiness(
            mode="redocking",
            ligands=ligands,
            receptors=receptors,
            ready_complex_ids=ready_complex_ids,
            total_complexes=len(complexes),
        )

    def conflict(
        self,
        signature: str,
        launched_signatures: Mapping[str, str],
    ) -> tuple[str, str]:
        active = [
            job
            for job in self.runtime.list_jobs(statuses=ACTIVE_JOB_STATUSES)
            if "docking" in str(job.task_type or "").lower()
        ]
        if not active:
            return ("none", "")
        known = {launched_signatures.get(str(job.job_id)) for job in active}
        if signature in known:
            return ("error", "An identical docking job (same molecules and settings) is already queued or running.")
        return ("warning", "Another docking job is already queued or running with different settings.")


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "DockingReadiness",
    "DockingReadinessService",
    "EntityReadiness",
    "PairReadiness",
]
