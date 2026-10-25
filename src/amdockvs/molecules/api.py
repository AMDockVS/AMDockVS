from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator, Mapping

from sqlalchemy import and_, update as sql_update
from sqlmodel import select
from ms_flow.query import QuerySpec, db_count, db_pages
from ms_flow.selection import Selection

from amdockvs.core.normalize import merge_filter_mappings, normalize_ids, normalize_set_name
from amdockvs.molecules.scopes import MoleculeScope, as_molecule_scope, is_molecule_scope
from amdockvs.core.constants import TABLE_LIGAND_ACTIVITIES, TABLE_MOLECULES
from amdockvs.core.payloads import molecule_payload_id
from amdockvs.models import (
    BindingSite,
    ComplexRecord,
    LigandActivity,
    MoleculeModel,
    MoleculeRecord,
    MoleculeRepresentation,
    SetRecord,
)
from amdockvs.models.molecules import sanitize_molecule_extra_data
from amdockvs.molecules.fragments import fragment_entries_from_metadata
from amdockvs.project.sets import (
    MoleculeSetRef,
    create_molecule_snapshot_set,
    get_molecule_set,
    molecule_set_spec,
    prepared_molecules_spec,
    sync_all_molecule_in_set_flags,
)
from amdockvs.workflows.rules import apply_workflow_filters


def ensure_molecule_set_ref(runtime: Any, source, *, name: str) -> MoleculeSetRef:
    if isinstance(source, MoleculeSetRef):
        if source.job_id:
            status = runtime.wait_for_job(source.job_id)
            if status.status != "completed":
                raise RuntimeError(status)
        return source
    if is_molecule_scope(source):
        return runtime.molecules.create_set(source, name=name, kind="snapshot")
    return MoleculeSetRef(id=int(source), kind="snapshot")


def _molecule_spec(scope: MoleculeScope | Selection[Any]) -> QuerySpec:
    """MoleculeScope -> QuerySpec. Membership and preparation state live in other tables and are
    declared as subqueries: the DB resolves them and no id list is ever materialised."""
    scope = as_molecule_scope(scope)
    if scope is None:
        raise ValueError("A molecule scope is required.")
    filters = dict(scope.filters or {})
    in_specs: list[QuerySpec] = []
    if scope.source_set_id is not None:
        in_specs.append(molecule_set_spec(int(scope.source_set_id)))
    has_activity = filters.pop("has_activity", None)
    if has_activity is not None:
        activities = QuerySpec(table=TABLE_LIGAND_ACTIVITIES, fields=("molecule_id",))
        if has_activity:
            in_specs.append(activities)
        else:
            filters["id__not_in_subquery"] = activities
    prepared = filters.pop("prepared", None)
    # Which preparation family the flag refers to (the `family` of ligand_preps / receptor_preps). Defaults to "ad4",
    # the family Vina, gnina and AutoDock-GPU share (one PDBQT prep for all three).
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
        current_model: MoleculeModel | None = None  # the model current_payload_id points at (metrics, files)
        current_model_data: dict | None = None  # its data payload (plddt, ptm...), the PAE left out

    def get(self, molecule_id: int) -> MoleculeRecord | None:
        """Return one molecule without exposing the project session to callers."""
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            return session.get(MoleculeRecord, int(molecule_id))

    def excluded_ids(self) -> set[int]:
        """Every excluded molecule (`idx_mol_excluded` holds only those): a run's "before"."""
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            return set(session.exec(select(MoleculeRecord.id).where(MoleculeRecord.excluded == True)).all())  # noqa: E712

    def excluded_since(self, before: set[int], *, limit: int = 10_000) -> list[tuple[int, str, str]]:
        """``(id, name, reason)`` of the molecules excluded now but not in ``before`` — what a run
        failed. ponytail: a run alongside it mixes its failures in; per-job ids if that matters."""
        new = sorted(self.excluded_ids() - set(before))[: int(limit)]
        if not new:
            return []
        with self.runtime.workspace.project_db.get_session() as session:
            rows = session.exec(
                select(MoleculeRecord.id, MoleculeRecord.name, MoleculeRecord.exclusion_reason)
                .where(MoleculeRecord.id.in_(new))
                .order_by(MoleculeRecord.id)
            ).all()
        return [(int(i), str(name or ""), str(reason or "")) for i, name, reason in rows]

    def details(self, molecule_id: int) -> Details | None:
        """Load the complete Details-panel projection for a molecule."""
        self.runtime._require_active_project()
        resolved_id = int(molecule_id)
        with self.runtime.workspace.project_db.get_session() as session:
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
            structure = molecule_payload_id(molecule)
            current_model = None if structure is None else session.exec(
                select(MoleculeModel)
                .where(MoleculeModel.molecule_id == resolved_id)
                .where(MoleculeModel.payload_id == int(structure))
            ).first()
        model_data = None
        if current_model is not None and current_model.data_payload_id is not None:
            import json

            from amdockvs.core.payloads import read_structure

            model_data = json.loads(read_structure(self.runtime.workspace.project_db, current_model.data_payload_id)[0])
            model_data.pop("pae", None)
        return self.Details(
            molecule=molecule,
            binding_sites=binding_sites,
            receptor_complexes=receptor_complexes,
            ligand_complexes=ligand_complexes,
            activities=activities,
            current_model=current_model,
            current_model_data=model_data,
        )

    def list_sets(self) -> list[SetRecord]:
        """List saved molecule sets newest first."""
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            return list(session.exec(select(SetRecord).order_by(SetRecord.id.desc())).all())

    def resolve_set(self, source: MoleculeSetRef | SetRecord | int) -> MoleculeSetRef:
        """Resolve UI/catalog set values into the public typed set reference."""
        self.runtime._require_active_project()
        if isinstance(source, MoleculeSetRef):
            return source
        set_id = int(source.id if isinstance(source, SetRecord) else source)
        record = get_molecule_set(self.runtime.workspace.project_db, set_id)
        if record is None:
            raise ValueError(f"Molecule set {set_id} does not exist.")
        return MoleculeSetRef(id=set_id, kind="snapshot")

    def sync_set_membership(self) -> None:
        """Reconcile the denormalized ``in_set`` molecule flag."""
        self.runtime._require_active_project()
        sync_all_molecule_in_set_flags(self.runtime.workspace.project_db)

    def scope_clause(self, scope: MoleculeScope | Selection[Any]):
        """Opaque SmartTable adapter for a public molecule scope.

        ORM expressions stay behind the AMDock API; UI callers only pass the returned value to
        SmartTableView's external-clause hook.
        """
        self.runtime._require_active_project()
        from amdockvs.molecules import filtering as filter_sql

        conditions = filter_sql.scope_conditions(self.runtime.workspace.project_db, as_molecule_scope(scope))
        return and_(*conditions) if conditions else None

    def evaluate_filter(
        self,
        scope: MoleculeScope | Selection[Any],
        criteria,
        *,
        exclusion_reason_prefix: str = "",
    ) -> dict[str, int]:
        """Count filter outcomes without mutating molecules."""
        self.runtime._require_active_project()
        from amdockvs.molecules import filtering as filter_sql

        conditions = filter_sql.scope_conditions(self.runtime.workspace.project_db, as_molecule_scope(scope))
        prefix = str(exclusion_reason_prefix or "").strip()
        if prefix:
            conditions.append(MoleculeRecord.exclusion_reason.like(f"{prefix}%"))
        return filter_sql.counts(self.runtime.workspace.project_db, conditions, criteria)

    def apply_filter(
        self,
        scope: MoleculeScope | Selection[Any],
        criteria,
        *,
        action: str,
        reason: str = "",
        exclusion_reason_prefix: str = "",
        set_name: str = "",
    ) -> tuple[int, int | None]:
        """Apply an evaluated filter or persist its matches as a molecule set."""
        self.runtime._require_active_project()
        from amdockvs.molecules import filtering as filter_sql

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"enrich", "recover", "tag"}:
            raise ValueError(f"Unsupported molecule-filter action: {action!r}")
        conditions = filter_sql.scope_conditions(self.runtime.workspace.project_db, as_molecule_scope(scope))
        prefix = str(exclusion_reason_prefix or "").strip()
        if prefix:
            conditions.append(MoleculeRecord.exclusion_reason.like(f"{prefix}%"))
        if normalized_action == "tag":
            ids = filter_sql.matched_ids(self.runtime.workspace.project_db, conditions, criteria)
            ref = self.create_set(
                ids,
                name=set_name,
                kind="filter",
                metadata={"source": str(reason or "")},
            )
            return len(ids), int(ref.id)
        return filter_sql.apply_state(
            self.runtime.workspace.project_db,
            conditions,
            criteria,
            activate_matches=normalized_action == "recover",
            exclude_nonmatches=normalized_action == "enrich",
            reason=str(reason or ""),
        )

    def delete(self, molecule_ids: Iterable[int | str]) -> int:
        self.runtime._require_active_project()
        from amdockvs.project.deletion import delete_molecules

        return delete_molecules(self.runtime.workspace.project_db, normalize_ids(molecule_ids))

    def molblock(self, molecule_id: int) -> str:
        """The molecule's current structure as a molfile — what the 2D editor opens."""
        from rdkit import Chem

        from amdockvs.chemistry.structures import molecule_mol

        molecule = self.get(molecule_id)
        mol = molecule_mol(self.runtime.workspace.project_db, molecule) if molecule is not None else None
        if mol is None:
            raise ValueError(f"Molecule {molecule_id} has no structure.")
        return Chem.MolToMolBlock(mol)

    def dependents(self, molecule_id: int) -> dict[str, int]:
        """What hangs from this molecule's structure and would go if an edit replaced it."""
        from sqlalchemy import func, or_

        from amdockvs.models import DockingResultRecord
        from amdockvs.models.qsar import LigandActivity, QSARPrediction

        counts = {}
        with self.runtime.workspace.project_db.get_session() as session:
            for label, model, clause in (
                ("activities", LigandActivity, LigandActivity.molecule_id == molecule_id),
                ("docking results", DockingResultRecord, or_(DockingResultRecord.ligand_molecule_id == molecule_id,
                                                             DockingResultRecord.receptor_molecule_id == molecule_id)),
                ("QSAR predictions", QSARPrediction, QSARPrediction.molecule_id == molecule_id),
            ):
                counts[label] = int(session.exec(select(func.count()).select_from(model).where(clause)).one())
        return {label: n for label, n in counts.items() if n}

    def edit(self, molecule_id: int, molblock: str, *, copy: bool = False) -> int | None:
        """Give a molecule an edited structure (a molfile from the 2D editor).

        `copy=False` replaces it: everything computed from the old structure goes (`clear_derived`:
        conformers, engine preparations, descriptors, fingerprints, docking results...) and so do its
        activities, measured on the old compound; the imported original (stored_payload_id), sets and
        complexes stay. `copy=True` leaves the molecule alone and adds the edit as a new one, with
        nothing of the old one's but its role. Returns the id holding the edit, or None when the
        drawing is still the same compound, which changes nothing."""
        self.runtime._require_active_project()
        from sqlalchemy import delete as sql_delete

        from amdockvs.chemistry.structures import molecule_mol, sdf_payload
        from amdockvs.core.payloads import payload_store
        from amdockvs.io.rows import identity_representations, structure_fields
        from amdockvs.models.qsar import LigandActivity
        from amdockvs.molecules.editing import edited_structure
        from amdockvs.project.deletion import clear_derived

        project_db = self.runtime.workspace.project_db
        molecule = self.get(molecule_id)
        if molecule is None:
            raise ValueError(f"Molecule {molecule_id} does not exist.")
        mol = edited_structure(molblock, molecule_mol(project_db, molecule))
        if mol is None:
            return None
        fields = structure_fields(mol)
        identity = {key: fields.pop(key) for key in ("inchikey", "canonical_smiles")}
        store = payload_store(project_db)
        if not copy:
            clear_derived(project_db, [molecule.id])
        with project_db.get_session() as session:
            edited_payload, = store.put(session, [sdf_payload(mol)])
            if copy:
                parent = session.get(MoleculeRecord, int(molecule.id))
                row = MoleculeRecord(**parent.model_dump(exclude={"id", "ro5_violations"}))
                row.name = f"{parent.name} (edited)"
                row.stored_payload_id = edited_payload  # the edit is all this molecule ever had
                row.in_set = False
                metadata = {"edited_from": int(parent.id)}
                replaced = None
            else:
                session.exec(sql_delete(LigandActivity).where(LigandActivity.molecule_id == int(molecule.id)))
                row = session.get(MoleculeRecord, int(molecule.id))
                metadata = sanitize_molecule_extra_data(row.extra_data)
                for stale in ("chemistry", "history", "state"):  # described the structure that is gone
                    metadata.pop(stale, None)
                replaced = row.current_payload_id
            row.current_payload_id = edited_payload
            for key, value in fields.items():
                setattr(row, key, value)
            row.is_minimized = False
            metadata["edited"] = True
            row.extra_data = metadata
            session.add(row)
            session.flush()
            for representation in identity_representations(identity, molecule_id=int(row.id)):
                session.merge(MoleculeRepresentation(**representation))
            session.commit()
            edited_id = int(row.id)
        if replaced:
            store.release([replaced])  # an earlier edit's bytes; the import is still referenced, so it stays
        return edited_id

    def _selection(self, scope: MoleculeScope) -> Selection[MoleculeRecord]:
        return Selection(
            scope=scope,
            _stream=lambda: self._stream_scope(scope),
            _count=lambda: self._count_scope(scope),
        )

    def all(
        self, *, source: MoleculeSetRef | int | None = None, order: tuple[str, ...] = ("id",)
    ) -> Selection[MoleculeRecord]:
        self.runtime._require_active_project()
        source_set_id = int(source.id if isinstance(source, MoleculeSetRef) else source) if source is not None else None
        return self._selection(MoleculeScope(filters={}, source_set_id=source_set_id, order=tuple(order)))

    def filter(
        self,
        scope: MoleculeScope | Selection[MoleculeRecord] | None = None,
        *,
        filters: Mapping[str, Any] | None = None,
        order: tuple[str, ...] | None = None,
    ) -> Selection[MoleculeRecord]:
        self.runtime._require_active_project()
        base = as_molecule_scope(scope) if scope is not None else as_molecule_scope(self.all())
        assert base is not None
        return self._selection(MoleculeScope(
            filters=merge_filter_mappings(base.filters, filters),
            source_set_id=base.source_set_id,
            order=tuple(order or base.order),
            limit=base.limit,
        ))

    def _stream_scope(self, scope: MoleculeScope) -> Iterator[MoleculeRecord]:
        self.runtime._require_active_project()
        spec = _molecule_spec(scope)
        for row in db_pages(self.runtime.workspace.project_db, spec):
            yield MoleculeRecord.model_validate(row)

    def stream(self, scope: MoleculeScope | Selection[MoleculeRecord] | None = None) -> Iterator[MoleculeRecord]:
        """Compatibility entry point; prefer iterating the returned Selection."""
        resolved = as_molecule_scope(scope) if scope is not None else as_molecule_scope(self.all())
        assert resolved is not None
        yield from self._stream_scope(resolved)

    def stream_ids(self, scope: MoleculeScope | Selection[MoleculeRecord] | None = None) -> Iterator[int]:
        self.runtime._require_active_project()
        resolved = as_molecule_scope(scope) if scope is not None else as_molecule_scope(self.all())
        assert resolved is not None
        spec = _molecule_spec(resolved)
        spec = replace(spec, fields=("id",))
        for row in db_pages(self.runtime.workspace.project_db, spec):
            yield int(row.get("id") or 0)

    def _count_scope(self, scope: MoleculeScope) -> int:
        return db_count(self.runtime.workspace.project_db, _molecule_spec(scope))

    def count(self, source: MoleculeSetRef | MoleculeScope | Selection[MoleculeRecord] | int | None = None) -> int:
        self.runtime._require_active_project()
        project_db = self.runtime.workspace.project_db
        if source is None or is_molecule_scope(source):
            resolved = as_molecule_scope(source) if source is not None else as_molecule_scope(self.all())
            assert resolved is not None
            return self._count_scope(resolved)
        set_id = int(source.id if isinstance(source, MoleculeSetRef) else source)
        return db_count(project_db, molecule_set_spec(set_id))

    def library_shape(self) -> tuple[bool, bool]:
        """`(the library is sharded, any ligand row exists)` — two one-row lookups.

        What the catalog bar needs to know and the only thing it needs the db for: Shards is
        there when there are shards, Ligands lights up when there is any ligand row at all.
        """
        from amdockvs.molecules.storage import has_ligand_rows, has_shards

        self.runtime._require_active_project()
        project_db = self.runtime.workspace.project_db
        return bool(has_shards(project_db)), has_ligand_rows(project_db)

    def shard_counts(self, *, engine: str = "ad4") -> dict[str, int]:
        """Inventory of the sharded library: shards, molecules in them, and how many are prepared.

        Preparation is engine-specific and does not rewrite the chemical inventory.
        """
        from amdockvs.molecules.storage import ShardStore, iter_prepared_shards, shard_scope_spec

        self.runtime._require_active_project()
        store = ShardStore(self.runtime.workspace.project_db)
        whole = shard_scope_spec(state=None)
        prepared = list(iter_prepared_shards(self.runtime.workspace.project_db, engine=engine))
        return {
            "shards": store.count(whole),
            "records": store.record_count(whole),
            "prepared": len(prepared),
            "prepared_records": sum(int(row.get("n_records") or 0) for row in prepared),
        }

    def list_fragments(self, molecule_id: int) -> list[dict[str, Any]]:
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(molecule_id))
        if molecule is None:
            raise ValueError(f"Molecule {molecule_id} does not exist.")
        return fragment_entries_from_metadata(sanitize_molecule_extra_data(molecule.extra_data))

    def select_fragment(self, molecule_id: int, fragment_index: int) -> Any:
        """Make another component of the imported original the molecule's current structure.

        Fragments are not stored: this one is cut again from the original and written alone. What
        was computed from the old structure goes (`clear_derived`); activities, measured on the
        imported compound, stay."""
        self.runtime._require_active_project()
        from amdockvs.chemistry.structures import molecule_mol, sdf_payload
        from amdockvs.core.payloads import payload_store
        from amdockvs.io.rows import identity_representations, structure_fields
        from amdockvs.molecules.fragments import fragments_of
        from amdockvs.project.deletion import clear_derived

        project_db = self.runtime.workspace.project_db
        molecule = self.get(molecule_id)
        if molecule is None:
            raise ValueError(f"Molecule {molecule_id} does not exist.")
        original = molecule_mol(project_db, {"stored_payload_id": molecule.stored_payload_id})
        if original is None:
            raise ValueError(f"Molecule {molecule_id} has no imported original to take a fragment from.")
        fragment = fragments_of(original).get(int(fragment_index))
        if fragment is None:
            raise ValueError(f"Fragment {fragment_index} does not exist for molecule {molecule_id}.")
        store = payload_store(project_db)
        clear_derived(project_db, [molecule.id])
        with project_db.get_session() as session:
            fragment_payload, = store.put(session, [sdf_payload(fragment)])
            row = session.get(MoleculeRecord, int(molecule.id))
            replaced = row.current_payload_id
            metadata = sanitize_molecule_extra_data(row.extra_data)
            if isinstance(metadata.get("fragmentation"), dict):  # a copy: in place, the JSON column sees no change
                metadata["fragmentation"] = {**metadata["fragmentation"], "selected_fragment_index": int(fragment_index)}
            for stale in ("chemistry", "history", "state"):  # described the structure that is gone
                metadata.pop(stale, None)
            row.extra_data = metadata
            row.current_payload_id = fragment_payload
            row.is_minimized = False
            fields = structure_fields(fragment)
            identity = {key: fields.pop(key) for key in ("inchikey", "canonical_smiles")}
            for key, value in fields.items():
                setattr(row, key, value)
            session.add(row)
            for representation in identity_representations(identity, molecule_id=int(row.id)):
                session.merge(MoleculeRepresentation(**representation))
            session.commit()
            session.refresh(row)
        if replaced:
            store.release([replaced])  # a no-op while it is still the import or a model's
        return row

    def select(
        self,
        *,
        source: MoleculeSetRef | int | None = None,
        molecule_kind: str | None = None,
        molecule_type: str | None = None,
        role: str | None = None,
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
    ) -> Selection[MoleculeRecord]:
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
        return self._selection(MoleculeScope(
            filters={key: value for key, value in filters.items() if value is not None},
            source_set_id=source_set_id,
            order=("id",),
            limit=limit,
        ))

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
        source: MoleculeScope | Selection[MoleculeRecord] | Iterable[int | str],
        *,
        name: str | None = None,
        kind: str = "manual",
        metadata: dict | None = None,
    ) -> MoleculeSetRef:
        self.runtime._require_active_project()
        molecule_ids = list(self.stream_ids(source)) if is_molecule_scope(source) else normalize_ids(source)
        return create_molecule_snapshot_set(
            self.runtime.workspace.project_db,
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
        with self.runtime.workspace.project_db.get_session() as session:
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
