from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from sqlalchemy import and_, update as sql_update
from sqlmodel import select
from ms_flow.query import QuerySpec, db_count, db_pages

from amdockvs.api_common import (
    MoleculeScope,
    merge_filter_mappings,
    normalize_ids,
    normalize_set_name,
)
from amdockvs.constants import TABLE_MOLECULES
from amdockvs.models import (
    BindingSite,
    ComplexRecord,
    LigandActivity,
    MoleculeRecord,
    SetRecord,
)
from amdockvs.models.molecules import sanitize_molecule_extra_data
from amdockvs.molecules.fragments import fragment_entries_from_metadata, fragment_entry_by_index
from amdockvs.molecule_paths import normalize_path, get_default_project_root
from amdockvs.scopes import (
    MoleculeSetRef,
    create_molecule_snapshot_set,
    get_molecule_set,
    molecule_set_spec,
    prepared_molecules_spec,
    sync_all_molecule_in_set_flags,
)
from amdockvs.workflows import apply_workflow_filters


def ensure_molecule_set_ref(runtime: Any, source, *, name: str) -> MoleculeSetRef:
    if isinstance(source, MoleculeSetRef):
        if source.job_id:
            status = runtime.wait_for_job(source.job_id)
            if status.status != "completed":
                raise RuntimeError(status)
        return source
    if isinstance(source, MoleculeScope):
        return runtime.molecules.create_set(source, name=name, kind="snapshot")
    return MoleculeSetRef(id=int(source), kind="snapshot")


def _molecule_spec(scope: MoleculeScope) -> QuerySpec:
    """MoleculeScope -> QuerySpec. Membership and preparation state live in other tables and are
    declared as subqueries: the DB resolves them and no id list is ever materialised."""
    filters = dict(scope.filters or {})
    in_specs: list[QuerySpec] = []
    if scope.source_set_id is not None:
        in_specs.append(molecule_set_spec(int(scope.source_set_id)))
    prepared = filters.pop("prepared", None)
    # Which preparation family the flag refers to (EngineState.engine). Defaults to "ad4",
    # the family Vina, gnina and AutoDock4 share (one PDBQT prep for all three).
    prep_engine = str(filters.pop("prepared_engine_key", "") or "ad4")
    if prepared is not None:
        role = ""
        if filters.get("is_ligand") is True:
            role = "ligand"
        elif filters.get("is_receptor") is True:
            role = "receptor"
        if role not in {"ligand", "receptor"}:
            # With no role there is no preparation family to query: empty scope, as before.
            filters["id"] = 0
        else:
            in_specs.append(
                prepared_molecules_spec(role_type=role, engine=prep_engine, is_ready=bool(prepared))
            )
    if in_specs:
        filters["id__in_subquery"] = in_specs
    return QuerySpec(
        table=TABLE_MOLECULES,
        filters=filters,
        order=tuple(scope.order or ()),
        limit=scope.limit,
    )


@dataclass
class MoleculeAPI:
    runtime: Any

    @dataclass(frozen=True)
    class Details:
        molecule: MoleculeRecord
        binding_sites: tuple[BindingSite, ...]
        receptor_complexes: tuple[ComplexRecord, ...]
        ligand_complexes: tuple[ComplexRecord, ...]
        activities: tuple[LigandActivity, ...]

    def get(self, molecule_id: int) -> MoleculeRecord | None:
        """Return one molecule without exposing the project session to callers."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            return session.get(MoleculeRecord, int(molecule_id))

    def details(self, molecule_id: int) -> Details | None:
        """Load the complete Details-panel projection for a molecule."""
        self.runtime._require_active_project()
        resolved_id = int(molecule_id)
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, resolved_id)
            if molecule is None:
                return None
            binding_sites = tuple(session.exec(
                select(BindingSite)
                .where(BindingSite.molecule_id == resolved_id)
                .order_by(BindingSite.id)
            ).all())
            receptor_complexes = tuple(session.exec(
                select(ComplexRecord)
                .where(ComplexRecord.receptor_molecule_id == resolved_id)
                .order_by(ComplexRecord.id.desc())
            ).all())
            ligand_complexes = tuple(session.exec(
                select(ComplexRecord)
                .where(ComplexRecord.ligand_molecule_id == resolved_id)
                .order_by(ComplexRecord.id.desc())
            ).all())
            activities = tuple(session.exec(
                select(LigandActivity)
                .where(LigandActivity.molecule_id == resolved_id)
                .order_by(LigandActivity.id.desc())
            ).all())
        return self.Details(
            molecule=molecule,
            binding_sites=binding_sites,
            receptor_complexes=receptor_complexes,
            ligand_complexes=ligand_complexes,
            activities=activities,
        )

    def list_sets(self) -> list[SetRecord]:
        """List saved molecule sets newest first."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            return list(session.exec(select(SetRecord).order_by(SetRecord.id.desc())).all())

    def resolve_set(self, source: MoleculeSetRef | SetRecord | int) -> MoleculeSetRef:
        """Resolve UI/catalog set values into the public typed set reference."""
        self.runtime._require_active_project()
        if isinstance(source, MoleculeSetRef):
            return source
        set_id = int(source.id if isinstance(source, SetRecord) else source)
        record = get_molecule_set(self.runtime.molsuite.project_db, set_id)
        if record is None:
            raise ValueError(f"Molecule set {set_id} does not exist.")
        return MoleculeSetRef(id=set_id, kind="snapshot")

    def sync_set_membership(self) -> None:
        """Reconcile the denormalized ``in_set`` molecule flag."""
        self.runtime._require_active_project()
        sync_all_molecule_in_set_flags(self.runtime.molsuite.project_db)

    def scope_clause(self, scope: MoleculeScope):
        """Opaque SmartTable adapter for a public molecule scope.

        ORM expressions stay behind the AMDock API; UI callers only pass the returned value to
        SmartTableView's external-clause hook.
        """
        self.runtime._require_active_project()
        from amdockvs.filtering import sql as filter_sql

        conditions = filter_sql.scope_conditions(self.runtime.molsuite.project_db, scope)
        return and_(*conditions) if conditions else None

    def evaluate_filter(
        self,
        scope: MoleculeScope,
        criteria,
        *,
        exclusion_reason_prefix: str = "",
    ) -> dict[str, int]:
        """Count filter outcomes without mutating molecules."""
        self.runtime._require_active_project()
        from amdockvs.filtering import sql as filter_sql

        conditions = filter_sql.scope_conditions(self.runtime.molsuite.project_db, scope)
        prefix = str(exclusion_reason_prefix or "").strip()
        if prefix:
            conditions.append(MoleculeRecord.exclusion_reason.like(f"{prefix}%"))
        return filter_sql.counts(self.runtime.molsuite.project_db, conditions, criteria)

    def apply_filter(
        self,
        scope: MoleculeScope,
        criteria,
        *,
        action: str,
        reason: str = "",
        exclusion_reason_prefix: str = "",
        set_name: str = "",
    ) -> tuple[int, int | None]:
        """Apply an evaluated filter or persist its matches as a molecule set."""
        self.runtime._require_active_project()
        from amdockvs.filtering import sql as filter_sql

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"enrich", "recover", "tag"}:
            raise ValueError(f"Unsupported molecule-filter action: {action!r}")
        conditions = filter_sql.scope_conditions(self.runtime.molsuite.project_db, scope)
        prefix = str(exclusion_reason_prefix or "").strip()
        if prefix:
            conditions.append(MoleculeRecord.exclusion_reason.like(f"{prefix}%"))
        if normalized_action == "tag":
            ids = filter_sql.matched_ids(self.runtime.molsuite.project_db, conditions, criteria)
            ref = self.create_set(
                ids,
                name=set_name,
                kind="filter",
                metadata={"source": str(reason or "")},
            )
            return len(ids), int(ref.id)
        return filter_sql.apply_state(
            self.runtime.molsuite.project_db,
            conditions,
            criteria,
            activate_matches=normalized_action == "recover",
            exclude_nonmatches=normalized_action == "enrich",
            reason=str(reason or ""),
        )

    def delete(self, molecule_ids: Iterable[int | str]) -> int:
        self.runtime._require_active_project()
        from amdockvs.deletion import delete_molecules

        return delete_molecules(self.runtime.molsuite.project_db, normalize_ids(molecule_ids))

    def all(self, *, source: MoleculeSetRef | int | None = None, order: tuple[str, ...] = ("id",)) -> MoleculeScope:
        self.runtime._require_active_project()
        source_set_id = int(source.id if isinstance(source, MoleculeSetRef) else source) if source is not None else None
        return MoleculeScope(filters={}, source_set_id=source_set_id, order=tuple(order))

    def filter(
        self,
        scope: MoleculeScope | None = None,
        *,
        filters: Mapping[str, Any] | None = None,
        order: tuple[str, ...] | None = None,
    ) -> MoleculeScope:
        self.runtime._require_active_project()
        base = scope or self.all()
        return MoleculeScope(
            filters=merge_filter_mappings(base.filters, filters),
            source_set_id=base.source_set_id,
            order=tuple(order or base.order),
            limit=base.limit,
        )

    def stream(self, scope: MoleculeScope | None = None) -> Iterator[Any]:
        self.runtime._require_active_project()
        spec = _molecule_spec(scope or self.all())
        for row in db_pages(self.runtime.molsuite.project_db, spec):
            yield MoleculeRecord.model_validate(row)

    def stream_ids(self, scope: MoleculeScope | None = None) -> Iterator[int]:
        self.runtime._require_active_project()
        spec = _molecule_spec(scope or self.all())
        spec = replace(spec, fields=("id",))
        for row in db_pages(self.runtime.molsuite.project_db, spec):
            yield int(row.get("id") or 0)

    def count(self, source: MoleculeSetRef | MoleculeScope | int | None = None) -> int:
        self.runtime._require_active_project()
        project_db = self.runtime.molsuite.project_db
        if source is None or isinstance(source, MoleculeScope):
            return db_count(project_db, _molecule_spec(source or self.all()))
        set_id = int(source.id if isinstance(source, MoleculeSetRef) else source)
        return db_count(project_db, molecule_set_spec(set_id))

    def list_fragments(self, molecule_id: int) -> list[dict[str, Any]]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
        if molecule is None:
            raise ValueError(f"Molecule {molecule_id} does not exist.")
        return fragment_entries_from_metadata(sanitize_molecule_extra_data(molecule.extra_data))

    def get_fragment_path(self, molecule_id: int, fragment_index: int) -> Path | None:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
        if molecule is None:
            raise ValueError(f"Molecule {molecule_id} does not exist.")
        entry = fragment_entry_by_index(sanitize_molecule_extra_data(molecule.extra_data), int(fragment_index))
        if entry is None:
            return None
        path = normalize_path(entry.get("path"))
        if path is not None and path.is_absolute():
            return path
        project_root = get_default_project_root()
        return None if project_root is None or path is None else (project_root / path).resolve()

    def select_fragment(self, molecule_id: int, fragment_index: int) -> Any:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
            if molecule is None:
                raise ValueError(f"Molecule {molecule_id} does not exist.")
            metadata = sanitize_molecule_extra_data(molecule.extra_data)
            fragment_entry = fragment_entry_by_index(metadata, int(fragment_index))
            if fragment_entry is None:
                raise ValueError(f"Fragment {fragment_index} does not exist for molecule {molecule_id}.")
            fragment_block = metadata.get("fragmentation")
            if not isinstance(fragment_block, dict):
                raise ValueError(f"Molecule {molecule_id} does not define fragment metadata.")
            fragment_block["selected_fragment_index"] = int(fragment_index)
            metadata["fragmentation"] = fragment_block
            molecule.extra_data = metadata
            molecule.current_path = str(fragment_entry.get("path") or molecule.current_path or "")
            properties = dict(fragment_entry.get("properties") or {})
            state = dict(fragment_entry.get("state") or {})
            molecule.n_atoms = int(properties.get("n_atoms") or molecule.n_atoms or 0)
            molecule.mw = properties.get("mw")
            molecule.exact_mw = properties.get("exact_mw")
            molecule.logp = properties.get("logp")
            molecule.hbd = properties.get("hbd")
            molecule.hba = properties.get("hba")
            molecule.tpsa = properties.get("tpsa")
            molecule.rotatable_bonds = properties.get("rotatable_bonds")
            molecule.fragment_count = properties.get("fragment_count")
            molecule.ring_count = properties.get("ring_count")
            molecule.aromatic_ring_count = properties.get("aromatic_ring_count")
            molecule.hetero_atom_count = properties.get("hetero_atom_count")
            molecule.heavy_atom_count = properties.get("heavy_atom_count")
            molecule.formal_charge = properties.get("formal_charge")
            molecule.fraction_csp3 = properties.get("fraction_csp3")
            molecule.pains_matches = list(properties.get("pains_matches") or [])
            molecule.ro5_violations = list(properties.get("ro5_violations") or [])
            molecule.has_3d = bool(state.get("has_3d", molecule.has_3d))
            molecule.has_hs = bool(state.get("has_hs", molecule.has_hs))
            molecule.conformer_count = int(state.get("conformer_count", molecule.conformer_count or 0) or 0)
            session.add(molecule)
            session.commit()
            session.refresh(molecule)
            return molecule

    def select(
        self,
        *,
        source: MoleculeSetRef | int | None = None,
        molecule_kind: str | None = None,
        molecule_type: str | None = None,
        role: str | None = None,
        context: str | None = None,
        workflow: str | None = None,
        usage_class: str | Iterable[str] | None = None,
        has_3d: bool | None = None,
        has_hs: bool | None = None,
        prepared: bool | None = None,
        has_activity: bool | None = None,
        excluded: bool | None = None,
        in_set: bool | None = None,
        n_atoms_gt: int | None = None,
        n_atoms_gte: int | None = None,
        n_atoms_lt: int | None = None,
        n_atoms_lte: int | None = None,
        mw_min: float | None = None,
        mw_max: float | None = None,
        limit: int | None = None,
    ) -> MoleculeScope:
        self.runtime._require_active_project()
        source_set_id = int(source.id if isinstance(source, MoleculeSetRef) else source) if source is not None else None
        normalized_role = str(role or "").strip().lower() or None
        resolved_molecule_type = str(molecule_type or molecule_kind or "").strip() or None
        resolved_usage_class = self._normalize_usage_class_filter(usage_class)
        effective_excluded = excluded
        if effective_excluded is None and normalized_role in {"ligand", "receptor"}:
            effective_excluded = False
        if resolved_usage_class is None and normalized_role in {"ligand", "receptor"}:
            resolved_usage_class = "general"
        filters: dict[str, Any] = {
            "molecule_type": resolved_molecule_type,
            "primary_context": context,
            "has_3d": has_3d,
            "has_hs": has_hs,
            "prepared": prepared,
            "has_activity": has_activity,
            "in_set": in_set,
            "n_atoms__gt": n_atoms_gt,
            "n_atoms__gte": n_atoms_gte,
            "n_atoms__lt": n_atoms_lt,
            "n_atoms__lte": n_atoms_lte,
            "mw__gte": mw_min,
            "mw__lte": mw_max,
        }
        if isinstance(resolved_usage_class, tuple):
            filters["usage_class__in"] = list(resolved_usage_class)
        elif resolved_usage_class is not None:
            filters["usage_class"] = resolved_usage_class
        if normalized_role == "ligand":
            filters["is_ligand"] = True
        elif normalized_role == "receptor":
            filters["is_receptor"] = True
        if workflow is not None:
            if not normalized_role:
                raise ValueError("select(..., workflow=...) requires role.")
            filters = apply_workflow_filters(filters, workflow=workflow, role=str(normalized_role))
        if effective_excluded is False:
            filters["excluded"] = False
        elif effective_excluded is True:
            filters["excluded"] = True
        return MoleculeScope(
            filters={key: value for key, value in filters.items() if value is not None},
            source_set_id=source_set_id,
            order=("id",),
            limit=limit,
        )

    @staticmethod
    def _normalize_usage_class_filter(value: str | Iterable[str] | None) -> str | tuple[str, ...] | None:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = str(value or "").strip()
            return normalized or None
        normalized_values = tuple(
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        )
        if not normalized_values:
            return None
        if len(normalized_values) == 1:
            return normalized_values[0]
        return normalized_values

    def create_set(
        self,
        source: MoleculeScope | Iterable[int | str],
        *,
        name: str | None = None,
        kind: str = "manual",
        metadata: dict | None = None,
    ) -> MoleculeSetRef:
        self.runtime._require_active_project()
        molecule_ids = list(self.stream_ids(source)) if isinstance(source, MoleculeScope) else normalize_ids(source)
        return create_molecule_snapshot_set(
            self.runtime.molsuite.project_db,
            name=normalize_set_name(name, fallback="molecule_set"),
            molecule_ids=molecule_ids,
            kind=kind,
            metadata=metadata,
        )

    def set_excluded_state(
        self,
        molecule_ids: Iterable[int | str],
        *,
        excluded: bool,
        reason: str = "",
        chunk_size: int = 1000,
    ) -> int:
        self.runtime._require_active_project()
        resolved_ids = normalize_ids(molecule_ids)
        if not resolved_ids:
            return 0
        updated = 0
        values = {
            "excluded": bool(excluded),
            "exclusion_reason": str(reason or "") if bool(excluded) else "",
        }
        with self.runtime.molsuite.project_db.get_session() as session:
            for start in range(0, len(resolved_ids), max(1, int(chunk_size))):
                chunk = resolved_ids[start : start + max(1, int(chunk_size))]
                result = session.exec(
                    sql_update(MoleculeRecord)
                    .where(MoleculeRecord.id.in_(chunk))
                    .values(**values)
                )
                updated += int(getattr(result, "rowcount", 0) or 0)
            session.commit()
        return updated


__all__ = [
    "MoleculeAPI",
    "MoleculeScope",
    "MoleculeSetRef",
    "ensure_molecule_set_ref",
]
