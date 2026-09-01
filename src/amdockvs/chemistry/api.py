from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math
from uuid import uuid4

from amdockvs.chemistry.jobs import (
    LigandChemistryJobParams,
    ReceptorChemistryJobParams,
    ligand_chemistry_job,
    receptor_chemistry_job,
)
from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.scopes import MoleculeSetRef
from amdockvs.summaries import JobStatus
from amdockvs.api_common import MoleculeScope, scope_payload
from amdockvs.chemistry.protonation_runtime import (
    ProtonationToolStatus,
    install_protonation_tool,
    protonation_tool_status,
)


@dataclass
class ChemistryAPI:
    runtime: Any

    def small_molecule_protonation_tool_status(self, name: str) -> ProtonationToolStatus:
        return protonation_tool_status(self.runtime, name)

    def install_small_molecule_protonation_tool(self, name: str) -> ProtonationToolStatus:
        return install_protonation_tool(self.runtime, name)

    def _estimate_scope_count(
        self,
        *,
        role: str,
        source: MoleculeSetRef | MoleculeScope | int | None,
    ) -> int | None:
        try:
            if isinstance(source, MoleculeScope):
                if str(role).strip():
                    role_key = "is_ligand" if str(role).strip() == "ligand" else "is_receptor"
                    scope = self.runtime.molecules.filter(source, filters={role_key: True})
                else:
                    scope = source
                return int(self.runtime.molecules.count(scope))
            scope = self.runtime.molecules.select(source=source, role=str(role).strip() or None)
            return int(self.runtime.molecules.count(scope))
        except Exception:
            return None

    def _submit_ligand_operation(
        self,
        operation: str,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        params: dict[str, Any] | None = None,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        self.runtime._require_active_project()
        ligand_set_ref = None if ligands is None or isinstance(ligands, MoleculeScope) else ensure_molecule_set_ref(self.runtime, ligands, name=f"chemistry_{operation}_input")
        ligand_scope = scope_payload(ligands) if isinstance(ligands, MoleculeScope) else {}
        ligand_filters = dict(ligand_scope.get("filters") or {})
        if "molecule_type" not in ligand_filters:
            # A scope stated by molecular type has already said what to act on; forcing the
            # role on top of it is what drops molecules whose type carries no role flag.
            ligand_filters["is_ligand"] = True
        if ligand_scope.get("limit") is not None:
            ligand_filters["_limit"] = ligand_scope.get("limit")
        total_items = self._estimate_scope_count(role="ligand", source=ligands)
        total_chunks = None if total_items is None else max(1, math.ceil(total_items / max(1, int(batch_size))))
        operation_params = dict(params or {})
        operation_params["structure_source"] = str(structure_source or "current")
        operation_params["run_id"] = uuid4().hex
        job_id = self.runtime.submit_job(
            ligand_chemistry_job,
            params=LigandChemistryJobParams(
                operation=operation,
                batch_size=batch_size,
                ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
                ligand_filters=ligand_filters,
                params=operation_params,
            ).model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            total_chunks=total_chunks,
        )
        if wait:
            return self.runtime.wait_for_job(job_id)
        return job_id

    def _submit_receptor_operation(
        self,
        operation: str,
        *,
        receptors: MoleculeSetRef | MoleculeScope | int | None = None,
        params: dict[str, Any] | None = None,
        structure_source: str = "current",
        batch_size: int = 32,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        self.runtime._require_active_project()
        receptor_set_ref = None if receptors is None or isinstance(receptors, MoleculeScope) else ensure_molecule_set_ref(self.runtime, receptors, name=f"chemistry_receptor_{operation}_input")
        receptor_scope = scope_payload(receptors) if isinstance(receptors, MoleculeScope) else {}
        receptor_filters = dict(receptor_scope.get("filters") or {})
        if "molecule_type" not in receptor_filters:
            receptor_filters["is_receptor"] = True  # same rule as ligands above
        if receptor_scope.get("limit") is not None:
            receptor_filters["_limit"] = receptor_scope.get("limit")
        total_items = self._estimate_scope_count(role="receptor", source=receptors)
        total_chunks = None if total_items is None else max(1, math.ceil(total_items / max(1, int(batch_size))))
        operation_params = dict(params or {})
        operation_params["structure_source"] = str(structure_source or "current")
        operation_params["run_id"] = uuid4().hex
        job_id = self.runtime.submit_job(
            receptor_chemistry_job,
            params=ReceptorChemistryJobParams(
                operation=operation,
                batch_size=batch_size,
                receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
                receptor_filters=receptor_filters,
                params=operation_params,
            ).model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            total_chunks=total_chunks,
        )
        if wait:
            return self.runtime.wait_for_job(job_id)
        return job_id

    def standardize_ligands(
        self,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        fragment_parent: bool = True,
        fragment_mode: str | None = None,
        neutralize: bool = True,
        canonicalize_tautomer: bool = False,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_ligand_operation(
            "standardize",
            ligands=ligands,
            params={
                "fragment_parent": bool(fragment_parent),
                "fragment_mode": None if fragment_mode is None else str(fragment_mode),
                "neutralize": bool(neutralize),
                "canonicalize_tautomer": bool(canonicalize_tautomer),
            },
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def protonate_ligands(
        self,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        method: str = "dimorphite",
        ph: float = 7.4,
        model: str = "molgpka",
        threads: int = 1,
        gpu: bool = False,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        normalized_method = str(method or "dimorphite").strip().lower()
        if normalized_method not in {"dimorphite", "openbabel", "pkasso", "polar_hydrogens"}:
            raise ValueError(f"Unsupported small-molecule protonation method: {method}")
        if not 0.0 <= float(ph) <= 14.0:
            raise ValueError("pH must be between 0 and 14.")
        normalized_model = str(model or "molgpka").strip().lower()
        if normalized_model not in {"molgpka", "mixed"}:
            raise ValueError("pKasso model must be 'molgpka' or 'mixed'.")
        use_gpu = bool(gpu and normalized_method == "pkasso" and normalized_model == "mixed")
        params: dict[str, Any] = {
            "method": normalized_method,
            "ph": float(ph),
            "model": normalized_model,
            "threads": max(1, int(threads)),
            "gpu": use_gpu,
        }
        if normalized_method in {"openbabel", "pkasso"}:
            status = protonation_tool_status(self.runtime, normalized_method)
            if not status.installed or status.command is None:
                raise RuntimeError(f"{status.message} Install its runtime from Build first.")
            params["tool_command"] = str(status.command)
        return self._submit_ligand_operation(
            "protonate",
            ligands=ligands,
            params=params,
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def generate_3d_ligands(
        self,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        add_hs: bool = True,
        random_seed: int = 0xF00D,
        optimize: bool = True,
        fragment_mode: str = "keep",
        filter_metals: bool = False,
        filter_simple_ions: bool = False,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_ligand_operation(
            "generate_3d",
            ligands=ligands,
            params={
                "add_hs": add_hs,
                "random_seed": random_seed,
                "optimize": optimize,
                "fragment_mode": str(fragment_mode or "largest_organic"),
                "filter_metals": filter_metals,
                "filter_simple_ions": filter_simple_ions,
            },
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def minimize_ligands(
        self,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        forcefield: str = "mmff",
        max_iters: int = 200,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_ligand_operation(
            "minimize",
            ligands=ligands,
            params={"forcefield": str(forcefield), "max_iters": int(max_iters)},
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def generate_ligand_conformers(
        self,
        *,
        ligands: MoleculeSetRef | MoleculeScope | int | None = None,
        num_conformers: int = 20,
        add_hs: bool = True,
        random_seed: int = 0xF00D,
        prune_rms_thresh: float = 0.5,
        optimize: bool = True,
        structure_source: str = "current",
        batch_size: int = 128,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_ligand_operation(
            "conformers",
            ligands=ligands,
            params={
                "num_conformers": int(num_conformers),
                "add_hs": bool(add_hs),
                "random_seed": int(random_seed),
                "prune_rms_thresh": float(prune_rms_thresh),
                "optimize": bool(optimize),
            },
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def protonate_receptors(
        self,
        *,
        receptors: MoleculeSetRef | MoleculeScope | int | None = None,
        method: str = "reduce",
        ph: float = 7.0,
        forcefield: str = "AMBER",
        structure_source: str = "current",
        batch_size: int = 32,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_receptor_operation(
            "protonate",
            receptors=receptors,
            params={"method": str(method), "ph": float(ph), "forcefield": str(forcefield)},
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def minimize_receptors(
        self,
        *,
        receptors: MoleculeSetRef | MoleculeScope | int | None = None,
        forcefields: tuple[str, ...] = ("amber14-all.xml",),
        max_iterations: int = 500,
        tolerance_kj_mol: float = 10.0,
        structure_source: str = "current",
        batch_size: int = 8,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_receptor_operation(
            "minimize",
            receptors=receptors,
            params={
                "forcefields": tuple(forcefields),
                "max_iterations": int(max_iterations),
                "tolerance_kj_mol": float(tolerance_kj_mol),
            },
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )

    def fix_receptors(
        self,
        *,
        receptors: MoleculeSetRef | MoleculeScope | int | None = None,
        add_missing_residues: bool = True,
        add_missing_atoms: bool = True,
        replace_nonstandard: bool = True,
        remove_heterogens: bool = False,
        keep_water: bool = True,
        structure_source: str = "current",
        batch_size: int = 8,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        wait: bool = False,
    ) -> str | JobStatus:
        return self._submit_receptor_operation(
            "fix",
            receptors=receptors,
            params={
                "add_missing_residues": bool(add_missing_residues),
                "add_missing_atoms": bool(add_missing_atoms),
                "replace_nonstandard": bool(replace_nonstandard),
                "remove_heterogens": bool(remove_heterogens),
                "keep_water": bool(keep_water),
            },
            structure_source=structure_source,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            wait=wait,
        )


__all__ = ["ChemistryAPI"]
