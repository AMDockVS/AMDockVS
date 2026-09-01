from __future__ import annotations

from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, or_
from sqlmodel import select

import amdockvs.docking.repository as repository
from amdockvs.constants import (
    DEFAULT_DOCKING_BATCH_SIZE,
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

# How many offending ids a requirement check reports. They exist to be read by a human in a
# dialog, and the caller gets the exact count separately.
MISSING_SAMPLE_SIZE = 10
from amdockvs.docking.programs import DockingProgramSpec, VINA_PROGRAM, get_docking_program, list_docking_programs
from amdockvs.docking.jobs import (
    count_pending_docking_pairs,
    count_pending_redocking_pairs,
    DiagramJobParams,
    DockingJobParams,
    InteractionJobParams,
    RedockingJobParams,
    diagram_job,
    docking_job,
    interactions_job,
    redocking_job,
)
from amdockvs.docking.preparation_jobs import (
    PreparationJobParams,
    prepare_ligands_job,
    prepare_receptors_job,
)
from amdockvs.api_common import MoleculeScope, PathLike, scope_payload
from amdockvs.docking.residues import box_from_coords, residues_in_box
from amdockvs.molecule_paths import preferred_molecule_path
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.models import BindingSite, EngineState, MoleculeRecord
from amdockvs.scopes import ComplexSetRef, MoleculeSetRef
from amdockvs.workflows import apply_workflow_filters


def _read_atom_coords(path: Path) -> list[tuple[float, float, float]]:
    """Atom coordinates from a ligand file. PDB/PDBQT via fixed columns; SDF/MOL via RDKit."""
    suffix = path.suffix.lower()
    if suffix in {".pdb", ".pdbqt", ".ent"}:
        coords: list[tuple[float, float, float]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                continue
            try:
                coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
            except (ValueError, IndexError):
                continue
        return coords
    from rdkit import Chem

    mol = (
        Chem.MolFromPDBFile(str(path), removeHs=False)
        if suffix == ".pdb"
        else next(iter(Chem.SDMolSupplier(str(path), sanitize=False, removeHs=False)), None)
        if suffix in {".sdf", ".mol"}
        else Chem.MolFromMol2File(str(path), sanitize=False, removeHs=False)
        if suffix == ".mol2"
        else None
    )
    if mol is None or mol.GetNumConformers() == 0:
        return []
    conf = mol.GetConformer()
    return [
        (float(p.x), float(p.y), float(p.z))
        for p in (conf.GetAtomPosition(i) for i in range(mol.GetNumAtoms()))
    ]


def _active_site(session, receptor) -> BindingSite | None:
    """The receptor's active site. A single FK hop: there is no index to match against."""
    site_id = int(getattr(receptor, "active_binding_site_id", 0) or 0)
    return None if site_id <= 0 else session.get(BindingSite, site_id)


@dataclass
class DockingAPI:
    runtime: Any

    def _project_db(self):
        self.runtime._require_active_project()
        return self.runtime.molsuite.project_db

    def list_programs(self) -> tuple[DockingProgramSpec, ...]:
        return list_docking_programs()

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
        # Counted over the id stream in bound-parameter-sized chunks: a scope of a million ligands
        # produces three integers here, not a million-element list.
        ids = (int(value) for value in self.runtime.molecules.stream_ids(scope) if int(value) > 0)
        total = ready = failed = 0
        with self.runtime.molsuite.project_db.get_session() as session:
            for chunk in batched(ids, 900):  # stay well under SQLite's bound-parameter limit
                total += len(chunk)
                rows = session.exec(
                    select(EngineState.is_ready)
                    .where(EngineState.molecule_id.in_(chunk))
                    .where(EngineState.role_type == normalized_role)
                    .where(EngineState.engine == normalized_engine)
                ).all()
                ready += sum(1 for value in rows if bool(value))
                failed += sum(1 for value in rows if not bool(value))
        return {"total": total, "ready": ready, "failed": failed}

    def unprepared_molecules_clause(self, *, role_type: str, engines: list[str] | tuple[str, ...]):
        """Opaque SmartTable clause selecting molecules not ready for every engine family."""
        self.runtime._require_active_project()
        role = str(role_type or "").strip().lower()
        if role not in {"ligand", "receptor"}:
            raise ValueError("role_type must be 'ligand' or 'receptor'.")
        normalized_engines = tuple(
            dict.fromkeys(str(value or "").strip().lower() for value in engines if str(value or "").strip()))
        missing = [
            ~exists(
                select(EngineState.id).where(
                    EngineState.molecule_id == MoleculeRecord.id,
                    EngineState.role_type == role,
                    EngineState.engine == engine,
                    EngineState.is_ready.is_(True),
                )
            )
            for engine in normalized_engines
        ]
        if not missing:
            return None
        return missing[0] if len(missing) == 1 else or_(*missing)

    def delete_results(self, result_ids: list[int] | tuple[int, ...]) -> int:
        self.runtime._require_active_project()
        from amdockvs.deletion import delete_docking_results

        return delete_docking_results(self.runtime.molsuite.project_db, result_ids)

    @staticmethod
    def get_program_spec(program: str | None = None) -> DockingProgramSpec:
        return get_docking_program(program)

    def list_binding_sites(self, *, molecule_id: int) -> list[BindingSite]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(
                select(BindingSite)
                .where(BindingSite.molecule_id == int(molecule_id))
                .order_by(BindingSite.id)
            ).all()
        return list(rows)

    def save_binding_site(
            self,
            *,
            molecule_id: int,
            name: str = "",
            source: str = "manual",
            source_ref: str = "",
            center: tuple[float, float, float],
            size: tuple[float, float, float],
            binding_site_id: int | None = None,
            set_active: bool = False,
            extra_data: dict[str, Any] | None = None,
    ) -> BindingSite:
        """Saves a site. Without `binding_site_id` it creates a new one; with it, rewrites that one."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
            if molecule is None:
                raise ValueError(f"Molecule {molecule_id} does not exist.")
            site = None if binding_site_id is None else session.get(BindingSite, int(binding_site_id))
            if site is None:
                site = BindingSite(molecule_id=int(molecule_id))
            elif int(site.molecule_id) != int(molecule_id):
                raise ValueError(
                    f"Binding site {binding_site_id} belongs to molecule {site.molecule_id}."
                )
            site.name = str(name or "")
            site.source = str(source or "manual")
            site.source_ref = str(source_ref or "")
            site.center_x = float(center[0])
            site.center_y = float(center[1])
            site.center_z = float(center[2])
            site.size_x = float(size[0])
            site.size_y = float(size[1])
            site.size_z = float(size[2])
            site.extra_data = dict(extra_data or {})
            session.add(site)
            session.flush()
            if set_active:
                molecule.active_binding_site_id = int(site.id or 0) or None
                session.add(molecule)
            session.commit()
            session.refresh(site)
            return site

    def suggest_box_from_ligand(
            self,
            *,
            ligand_id: int,
            scale: float = 1.5,
            padding: float = 4.0,
    ) -> dict[str, Any]:
        """Auto box from a reference ligand: center = its centroid, size = cubic box
        derived from the ligand's radius of gyration. Returns {center, size, rg}."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            ligand = session.get(MoleculeRecord, int(ligand_id))
            if ligand is None:
                raise ValueError(f"Molecule {ligand_id} does not exist.")
            path = preferred_molecule_path(ligand)
        if path is None or not Path(path).exists():
            raise ValueError(f"Ligand {ligand_id} has no readable structure file.")
        coords = _read_atom_coords(Path(path))
        if not coords:
            raise ValueError(f"No atom coordinates found in {Path(path).name}.")
        return box_from_coords(coords, scale=float(scale), padding=float(padding))

    def set_active_binding_site(self, *, molecule_id: int, binding_site_id: int | None) -> None:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
            if molecule is None:
                raise ValueError(f"Molecule {molecule_id} does not exist.")
            if binding_site_id is not None:
                site = session.get(BindingSite, int(binding_site_id))
                if site is None or int(site.molecule_id) != int(molecule_id):
                    raise ValueError(
                        f"Binding site {binding_site_id} does not belong to molecule {molecule_id}."
                    )
            molecule.active_binding_site_id = None if binding_site_id is None else int(binding_site_id)
            session.add(molecule)
            session.commit()

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
        ligand_set_ref = None if ligand_set is None or isinstance(ligand_set,
                                                                  MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="prepare_ligands_check_input")
        ligand_scope = scope_payload(ligand_set) if isinstance(ligand_set, MoleculeScope) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        project_db = self.runtime.molsuite.project_db
        set_id = None if ligand_set_ref is None else int(ligand_set_ref.id)
        prep_engine = program_spec.preparation_engine
        total = count_entity_rows(
            project_db, entity_kind="ligand", engine=prep_engine, set_id=set_id, filters=ligand_filters
        )
        # The offenders are only ever counted or shown to a user, so fetch a sample, not the
        # whole list: on a million-ligand library the full list is both the slow part and an
        # unreadable error message.
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
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            batch_size: int | None = None,  # None -> settings (batch_sizes.ligand)
            force: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
    ) -> str:
        program_spec = self.get_program_spec(program)
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
                raise RuntimeError(
                    f"Missing has_3d for {missing_count} ligand(s): {shown}. "
                    "Run runtime.chemistry.generate_3d_ligands(...) first."
                )
        ligand_set_ref = None if ligand_set is None or isinstance(ligand_set,
                                                                  MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="prepare_ligands_input")
        ligand_scope = scope_payload(ligand_set) if isinstance(ligand_set, MoleculeScope) else {}
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

    def prepare_receptors(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
            batch_size: int | None = None,  # None -> settings (batch_sizes.receptor)
            force: bool = False,
            keep_waters: bool = False,
            keep_cofactors: bool = False,
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
    ) -> str:
        program_spec = self.get_program_spec(program)
        receptor_set_ref = None if receptor_set is None or isinstance(receptor_set,
                                                                      MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, receptor_set, name="prepare_receptors_input")
        receptor_scope = scope_payload(receptor_set) if isinstance(receptor_set, MoleculeScope) else {}
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
        site = self.save_binding_site(
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
        with self.runtime.molsuite.project_db.get_session() as session:
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

    def check_required(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
    ) -> dict[str, Any]:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        ligand_set_ref = None if ligand_set is None or isinstance(ligand_set,
                                                                  MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="docking_check_ligand_input")
        receptor_set_ref = None if receptor_set is None or isinstance(receptor_set,
                                                                      MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, receptor_set, name="docking_check_receptor_input")
        ligand_scope = scope_payload(ligand_set) if isinstance(ligand_set, MoleculeScope) else {}
        receptor_scope = scope_payload(receptor_set) if isinstance(receptor_set, MoleculeScope) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        project_db = self.runtime.molsuite.project_db
        # Ligands are counted, never listed: "prepared?" is a subquery, so a million-ligand
        # library costs two COUNTs and a sample. Receptors stay on list_entity_rows — there
        # are tens of them and the grid check needs the hydrated binding-site payload.
        ligand_scope = dict(
            entity_kind="ligand",
            engine=program_spec.preparation_engine,
            set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
        )
        unprepared_filters = {**ligand_filters, "prepared_engine": False}
        ligands_total = count_entity_rows(project_db, **ligand_scope, filters=ligand_filters)
        missing_ligands = entity_ids(
            project_db, **ligand_scope, filters=unprepared_filters, limit=MISSING_SAMPLE_SIZE + 1
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
        ready_receptor_ids = {int(row.get("id") or 0) for row in receptor_rows if bool(row.get("prepared_engine"))}
        ready_grid_receptor_ids = {int(row.get("id") or 0) for row in receptor_rows if bool(row.get("grid_engine"))}
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
        with self.runtime.molsuite.project_db.get_session() as session:
            receptor = session.get(MoleculeRecord, int(receptor_id))
        if receptor is None:
            return []
        path = preferred_molecule_path(receptor)
        if path is None or not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        return [
            {"key": r.key, "label": r.label, "chain": r.chain, "resname": r.resname, "resnum": r.resnum}
            for r in residues_in_box(text, center, size)
        ]

    def get_flexible_residues(self, *, receptor_id: int) -> list[str]:
        # Flexible residues belong to the receptor (what gets passed to prep), not to any
        # binding site — the box is only a candidate filter, not the owner of the selection.
        with self.runtime.molsuite.project_db.get_session() as session:
            receptor = session.get(MoleculeRecord, receptor_id)
            if receptor is None:
                return []
            return [str(k) for k in (dict(receptor.extra_data or {}).get("flexible_residues") or [])]

    def set_flexible_residues(self, *, receptor_id: int, residue_keys: list[str]) -> list[str]:
        keys = [str(k) for k in residue_keys]
        with self.runtime.molsuite.project_db.get_session() as session:
            receptor = session.get(MoleculeRecord, receptor_id)
            if receptor is None:
                raise ValueError(f"No receptor with id {receptor_id}.")
            data = dict(receptor.extra_data or {})
            data["flexible_residues"] = keys
            receptor.extra_data = data
            session.add(receptor)
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
            if receptor_set is None or isinstance(receptor_set, MoleculeScope)
            else ensure_molecule_set_ref(self.runtime, receptor_set, name="docking_check_receptor_input")
        )
        receptor_scope = scope_payload(receptor_set) if isinstance(receptor_set, MoleculeScope) else {}
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        receptor_rows = list_entity_rows(
            self.runtime.molsuite.project_db,
            entity_kind="receptor",
            engine=program_spec.preparation_engine,
            set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            filters=receptor_filters,
            fields=("id", "prepared_engine", "grid_engine"),
            order=("id",),
        )
        receptor_ids = [int(row.get("id") or 0) for row in receptor_rows if int(row.get("id") or 0) > 0]
        ready_prepared = {int(row.get("id") or 0) for row in receptor_rows if bool(row.get("prepared_engine"))}
        ready_grid = {int(row.get("id") or 0) for row in receptor_rows if bool(row.get("grid_engine"))}
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

    def _grid_from_receptor_set(
            self,
            receptor_set_ref: MoleculeSetRef | None,
            *,
            engine: str,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float], float] | None:
        if receptor_set_ref is None:
            return None
        receptor_ids = list_receptor_ids_in_set(
            self.runtime.molsuite.project_db,
            receptor_set_id=int(receptor_set_ref.id),
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

    def run(
            self,
            *,
            program: str = VINA_PROGRAM.key,
            ligand_set: MoleculeSetRef | MoleculeScope | int | None = None,
            receptor_set: MoleculeSetRef | MoleculeScope | int | None = None,
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
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
            skip_existing: bool = True,
            compute_diagram: bool = False,
            diagram_format: str = "png",
    ) -> str:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        ligand_set_ref = None if ligand_set is None or isinstance(ligand_set,
                                                                  MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, ligand_set, name="docking_ligand_input")
        receptor_set_ref = None if receptor_set is None or isinstance(receptor_set,
                                                                      MoleculeScope) else ensure_molecule_set_ref(
            self.runtime, receptor_set, name="docking_receptor_input")
        ligand_scope = scope_payload(ligand_set) if isinstance(ligand_set, MoleculeScope) else {}
        receptor_scope = scope_payload(receptor_set) if isinstance(receptor_set, MoleculeScope) else {}
        ligand_filters = self._program_scope_filters(ligand_scope, role="ligand", program=program_spec)
        receptor_filters = self._program_scope_filters(receptor_scope, role="receptor", program=program_spec)
        if box_center is None or box_size is None:
            resolved_grid = self._grid_from_receptor_set(receptor_set_ref, engine=program_spec.preparation_engine)
            if resolved_grid is not None:
                box_center, box_size, spacing = resolved_grid
        if (box_center is None) ^ (box_size is None):
            raise ValueError("docking.run requires both box_center and box_size when either one is provided.")
        # Always resolve counts: they gate readiness (when requested) AND give the true
        # chunk total so progress is computed against the real pair count instead of
        # completed/emitted-so-far (a lazy generator otherwise leaves total_chunks unknown,
        # making progress hover near 50-60% regardless of actual completion).
        requirement_check = self.check_required(
            program=program_spec.key,
            ligand_set=ligand_set if isinstance(ligand_set, MoleculeScope) else ligand_set_ref,
            receptor_set=receptor_set if isinstance(receptor_set, MoleculeScope) else receptor_set_ref,
        )
        if check_required and not bool(requirement_check.get("ready")):
            missing = dict(requirement_check.get("missing") or {})
            counts = dict(requirement_check.get("counts") or {})
            required_jobs = dict(requirement_check.get("required_jobs") or {})
            # `missing` holds capped samples, so the real numbers come from `counts`.
            raise RuntimeError(
                f"docking.run requirements are not satisfied for docking.{program_spec.key}. "
                f"counts={counts}; missing_sample={missing}; required_jobs={required_jobs}"
            )
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
            project_db=self.runtime.molsuite.project_db,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            ligand_filters=ligand_filters,
            receptor_filters=receptor_filters,
            protocol_metadata=protocol_metadata,
            skip_existing=skip_existing,
        )
        total_chunks = (
            -(-pending_pairs // max(1, int(batch_size))) if pending_pairs > 0 else 0
        )
        resolved_output = (
            str(Path(output_dir).expanduser().resolve())
            if output_dir is not None
            else str(self.runtime.get_project_resource_path("docking_results"))
        )
        resolved_run_id = str(run_id or uuid4().hex)
        params = DockingJobParams(
            output_dir=resolved_output,
            batch_size=batch_size,
            ligand_set_id=None if ligand_set_ref is None else int(ligand_set_ref.id),
            receptor_set_id=None if receptor_set_ref is None else int(receptor_set_ref.id),
            ligand_filters=ligand_filters,
            receptor_filters=receptor_filters,
            engine=program_spec.docking_engine,
            preparation_engine=program_spec.preparation_engine,
            exhaustiveness=exhaustiveness,
            num_modes=num_modes,
            box_center=box_center,
            box_size=box_size,
            scoring_function=scoring_function,
            vina_backend=vina_backend,
            vina_command=vina_command,
            vina_cpu=vina_cpu,
            seed=seed,
            spacing=spacing,
            energy_range=energy_range,
            min_rmsd=min_rmsd,
            run_id=resolved_run_id,
            protocol_metadata=dict(protocol_metadata or {}),
            skip_existing=skip_existing,
            compute_diagram=bool(compute_diagram),
            diagram_format=str(diagram_format or "png"),
        )
        return self.runtime.submit_job(
            docking_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            # Each docking chunk runs one engine process that uses `vina_cpu` threads
            # internally, so reserve that many CPU tokens from the pool to avoid
            # oversubscription (pool limits concurrency to floor(cpus / vina_cpu)).
            cpu_required=max(1, int(vina_cpu)),
            # Declare the real chunk total so progress = processed/total (not processed/emitted).
            total_chunks=total_chunks,
        )

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
            executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
            depends_on: list[str] | None = None,
            check_required: bool = True,
            skip_existing: bool = True,
            compute_diagram: bool = False,
            diagram_format: str = "png",
    ) -> str:
        self.runtime._require_active_project()
        program_spec = self.get_program_spec(program)
        complex_set_id = None if complex_set is None else int(
            complex_set.id if isinstance(complex_set, ComplexSetRef) else complex_set)
        explicit_complex_ids = sorted({int(value) for value in (complex_ids or []) if int(value) > 0})
        purpose_text = str(purpose or "redocking").strip() or "redocking"
        if check_required:
            complex_rows = list_complex_rows(
                self.runtime.molsuite.project_db,
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
                self.runtime.molsuite.project_db,
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
                if not bool(ligand_row.get("prepared_engine")):
                    missing["ligands_prepared"].append(ligand_id)
                if not bool(receptor_row.get("prepared_engine")):
                    missing["receptors_prepared"].append(receptor_id)
                if (box_center is None or box_size is None) and not bool(receptor_row.get("grid_engine")):
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
            project_db=self.runtime.molsuite.project_db,
            engine=program_spec.docking_engine,
            complex_set_id=complex_set_id,
            complex_ids=explicit_complex_ids,
            purpose=purpose_text,
            protocol_metadata=protocol_metadata,
            skip_existing=skip_existing,
        )
        total_chunks = (
            -(-pending_pairs // max(1, int(batch_size))) if pending_pairs > 0 else 0
        )
        resolved_output = (
            str(Path(output_dir).expanduser().resolve())
            if output_dir is not None
            else str(self.runtime.get_project_resource_path("docking_results"))
        )
        resolved_run_id = str(run_id or uuid4().hex)
        params = RedockingJobParams(
            output_dir=resolved_output,
            batch_size=batch_size,
            complex_set_id=complex_set_id,
            complex_ids=explicit_complex_ids,
            purpose=purpose_text,
            engine=program_spec.docking_engine,
            exhaustiveness=exhaustiveness,
            num_modes=num_modes,
            box_center=box_center,
            box_size=box_size,
            scoring_function=scoring_function,
            vina_backend=vina_backend,
            vina_command=vina_command,
            vina_cpu=vina_cpu,
            seed=seed,
            spacing=spacing,
            energy_range=energy_range,
            min_rmsd=min_rmsd,
            run_id=resolved_run_id,
            protocol_metadata=dict(protocol_metadata or {}),
            skip_existing=skip_existing,
            compute_diagram=bool(compute_diagram),
            diagram_format=str(diagram_format or "png"),
        )
        return self.runtime.submit_job(
            redocking_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
            cpu_required=max(1, int(vina_cpu)),
            total_chunks=total_chunks,
        )

    def list_results(self, *, limit: int = 5000, offset: int = 0):
        return repository.list_results(self._project_db(), limit=limit, offset=offset)

    def result_stats(self):
        return repository.get_docking_results_stats(self._project_db())

    def top_hits(self, *, limit: int = 25, receptor_id: int | None = None, only_completed: bool = True):
        return repository.list_top_hits(
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
        return repository.list_ligand_result_summaries(
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
        return repository.list_top_hits(
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
        return repository.get_hit(self._project_db(), result_id=int(result_id))

    def result_protocols(self, *, receptor_id: int | None = None, exclude_run_kind: str | None = "redocking"):
        return repository.list_result_protocols(
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
        from amdockvs.docking.diagram import diagram_path_for

        suffix = ".svg" if str(fmt).lower() == "svg" else ".png"
        return str(diagram_path_for(pose_path, pose_rank, suffix=suffix))

    def list_interactions(self, *, result_id: int):
        return repository.list_interactions(self._project_db(), result_id=int(result_id))

    def interaction_stats(self, *, result_ids: list[int] | None = None):
        return repository.interaction_stats(self._project_db(), result_ids=result_ids)

    def count_docked_pairs(
            self,
            *,
            engine: str = "",
            receptor_ids: list[int] | None = None,
            protocol_hash: str | None = None,
            run_kind: str | None = "screening",
    ) -> int:
        return repository.count_docked_pairs(
            self._project_db(),
            engine=engine,
            receptor_ids=receptor_ids,
            protocol_hash=protocol_hash,
            run_kind=run_kind,
        )

    def receptor_summaries(self):
        return repository.list_receptor_result_summaries(self._project_db())

    def pivot_availability(self) -> dict[str, bool]:
        return repository.pivot_availability(self._project_db())

    def offtarget_rows(
            self,
            *,
            receptor_ids: list[int] | None = None,
            ligand_ids: list[int] | None = None,
            limit: int = 50000,
    ):
        return repository.list_offtarget_rows(
            self._project_db(),
            receptor_ids=receptor_ids, ligand_ids=ligand_ids, limit=limit
        )


__all__ = ["DockingAPI"]
