from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from sqlmodel import select

from amdockvs.models import ComplexRecord, LigandActivity, MoleculeRecord
from amdockvs.vocab import ComplexPurpose
from amdockvs.scopes import ComplexRef, ComplexSetRef, create_complex_snapshot_set, list_complex_set_ids


@dataclass
class ComplexAPI:
    runtime: Any

    @dataclass(frozen=True)
    class Details:
        complex: ComplexRecord
        receptor: MoleculeRecord | None
        ligand: MoleculeRecord | None
        activity: LigandActivity | None

    def create(
        self,
        *,
        receptor_molecule_id: int,
        ligand_molecule_id: int,
        name: str = "",
        purpose: str = "redocking",
        reference_receptor_path: str = "",
        reference_ligand_path: str = "",
        binding_site_id: int | None = None,
        activity_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ComplexRef:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            record = ComplexRecord(
                name=str(name or "").strip() or f"complex_{int(receptor_molecule_id)}_{int(ligand_molecule_id)}",
                receptor_molecule_id=int(receptor_molecule_id),
                ligand_molecule_id=int(ligand_molecule_id),
                reference_receptor_path=str(reference_receptor_path or ""),
                reference_ligand_path=str(reference_ligand_path or ""),
                binding_site_id=None if binding_site_id is None else int(binding_site_id),
                activity_id=None if activity_id is None else int(activity_id),
                purpose=str(purpose or ComplexPurpose.REDOCKING).strip() or ComplexPurpose.REDOCKING,
                metadata_json=json.dumps(dict(metadata or {}), ensure_ascii=True),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return ComplexRef(id=int(record.id or 0))

    def get(self, complex_ref: ComplexRef | int) -> ComplexRecord | None:
        self.runtime._require_active_project()
        complex_id = int(complex_ref.id if isinstance(complex_ref, ComplexRef) else complex_ref)
        with self.runtime.molsuite.project_db.get_session() as session:
            return session.get(ComplexRecord, complex_id)

    def details(self, complex_ref: ComplexRef | int) -> Details | None:
        """Load the complete Details-panel projection for a receptor-ligand pair."""
        self.runtime._require_active_project()
        complex_id = int(complex_ref.id if isinstance(complex_ref, ComplexRef) else complex_ref)
        with self.runtime.molsuite.project_db.get_session() as session:
            pair = session.get(ComplexRecord, complex_id)
            if pair is None:
                return None
            receptor = session.get(MoleculeRecord, int(pair.receptor_molecule_id or 0))
            ligand = session.get(MoleculeRecord, int(pair.ligand_molecule_id or 0))
            activity = (
                session.get(LigandActivity, int(pair.activity_id or 0))
                if int(pair.activity_id or 0) > 0
                else None
            )
        return self.Details(complex=pair, receptor=receptor, ligand=ligand, activity=activity)

    def delete(self, complex_ids: Iterable[int | str]) -> int:
        self.runtime._require_active_project()
        from amdockvs.deletion import delete_complexes

        return delete_complexes(self.runtime.molsuite.project_db, complex_ids)

    def stream(
        self,
        *,
        complex_set: ComplexSetRef | int | None = None,
        purpose: str | None = None,
    ) -> Iterator[ComplexRecord]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            statement = select(ComplexRecord)
            if purpose is not None:
                allowed = {
                    value.strip()
                    for value in str(purpose or "").split(",")
                    if value.strip()
                }
                if allowed:
                    statement = statement.where(ComplexRecord.purpose.in_(sorted(allowed)))
            statement = statement.order_by(ComplexRecord.id)
            rows = list(session.exec(statement))
        allowed_ids = None
        if complex_set is not None:
            allowed_ids = set(list_complex_set_ids(self.runtime.molsuite.project_db, complex_set))
        for row in rows:
            row_id = int(row.id or 0)
            if allowed_ids is not None and row_id not in allowed_ids:
                continue
            yield row

    def list(
        self,
        *,
        complex_set: ComplexSetRef | int | None = None,
        purpose: str | None = None,
    ) -> list[ComplexRecord]:
        return list(self.stream(complex_set=complex_set, purpose=purpose))

    def count(
        self,
        *,
        complex_set: ComplexSetRef | int | None = None,
        purpose: str | None = None,
    ) -> int:
        return len(self.list(complex_set=complex_set, purpose=purpose))

    def create_set(
        self,
        source: Iterable[int | str] | ComplexRef | ComplexSetRef | int,
        *,
        name: str,
        kind: str = "snapshot",
        metadata: dict[str, Any] | None = None,
    ) -> ComplexSetRef:
        self.runtime._require_active_project()
        if isinstance(source, ComplexRef):
            complex_ids = [int(source.id)]
        elif isinstance(source, ComplexSetRef):
            complex_ids = list_complex_set_ids(self.runtime.molsuite.project_db, source)
        elif isinstance(source, int):
            complex_ids = [int(source)]
        else:
            complex_ids = [int(value) for value in source]
        return create_complex_snapshot_set(
            self.runtime.molsuite.project_db,
            name=str(name or "").strip() or "complex_set",
            complex_ids=complex_ids,
            kind=kind,
            metadata=metadata,
        )


__all__ = ["ComplexAPI"]
