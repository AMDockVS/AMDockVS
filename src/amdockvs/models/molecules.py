from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import Index, JSON, UniqueConstraint, text
from pydantic import field_validator
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_BINDING_SITES,
    TABLE_MOLECULE_MODELS,
    TABLE_MOLECULE_REPRESENTATIONS,
    TABLE_MOLECULE_SOURCE_PROPERTIES,
    TABLE_MOLECULES,
)
from .base import TimestampedRecord

# Domain vocabulary centralised in amdockvs.vocab. Re-exported here so existing
# imports keep working (`from amdockvs.models.molecules import MoleculeType`).
from amdockvs.vocab import (
    FileFormat,
    ModelSource,
    MoleculeType,
    MoleculeUsageClass,
    ReprType,
)


_RESERVED_EXTRA_DATA_KEYS = {"state", "project_root", "filtering", "chemistry"}


def sanitize_molecule_extra_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        raw_mapping = dict(value)
    else:
        text_value = str(value or "").strip()
        if not text_value:
            return {}
        try:
            parsed = json.loads(text_value)
        except json.JSONDecodeError:
            return {}
        raw_mapping = parsed if isinstance(parsed, dict) else {}
    return {
        str(key): item
        for key, item in raw_mapping.items()
        if str(key) not in _RESERVED_EXTRA_DATA_KEYS
    }


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    text_value = str(value or "").strip()
    if not text_value:
        return []
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


# ---------------------------------------------------------------------------
# MoleculeRecord
# ---------------------------------------------------------------------------

class MoleculeRecord(TimestampedRecord, table=True):
    """
    Central registry. One row per molecule regardless of role.

    Status logic:
        excluded=False, usage_class=general   → active general pool
        excluded=False, usage_class=reference → reference-only molecules (e.g. cocrystals)
        excluded=True                         → globally discarded

    Representations live in molecule_representations.
    3D models / conformers live in molecule_models.
    """

    __tablename__ = TABLE_MOLECULES
    __table_args__ = (
        Index("idx_mol_active", "id", sqlite_where=text("excluded=0 AND in_set=0")),
        Index("idx_mol_excluded", "id", sqlite_where=text("excluded=1")),
        Index("idx_mol_in_set", "id", sqlite_where=text("in_set=1")),
    )

    id: int | None = Field(default=None, primary_key=True)

    # Identity (immutable after ingestion)
    name: str = Field(default="", index=True)
    molecule_type: str = Field(default=MoleculeType.UNKNOWN)
    source: str = Field(default="")  # absolute path — lives outside project
    source_index: int = Field(default=0)
    input_format: str = Field(default=FileFormat.UNKNOWN)

    # Roles
    is_receptor: bool = Field(default=False)
    is_ligand: bool = Field(default=False)
    active_binding_site_id: int | None = Field(
        default=None,
        foreign_key=f"{TABLE_BINDING_SITES}.id",
    )

    # Paths relative to project root
    # stored_path → canonical imported copy
    # current_path → currently selected working model/conformer
    stored_path: str = Field(default="")
    current_path: str = Field(default="")
    current_model_index: int | None = Field(default=None)

    # Basic chemical properties
    n_atoms: int = Field(default=0)
    mw: float | None = Field(default=None)
    exact_mw: float | None = Field(default=None)
    logp: float | None = Field(default=None)
    hbd: int | None = Field(default=None)
    hba: int | None = Field(default=None)
    tpsa: float | None = Field(default=None)
    rotatable_bonds: int | None = Field(default=None)
    fragment_count: int | None = Field(default=None)
    ring_count: int | None = Field(default=None)
    aromatic_ring_count: int | None = Field(default=None)
    hetero_atom_count: int | None = Field(default=None)
    heavy_atom_count: int | None = Field(default=None)
    formal_charge: int | None = Field(default=None)
    fraction_csp3: float | None = Field(default=None)
    pains_matches: list[str] = Field(default_factory=list, sa_type=JSON)
    ro5_violations: list[str] = Field(default_factory=list, sa_type=JSON)
    conformer_count: int = Field(default=0)

    # Fast-check state flags — updated by the layer that performs each operation.
    # Avoid JOINs for pre-flight checks before launching batch jobs.
    has_3d: bool = Field(default=False)
    has_hs: bool = Field(default=False)
    is_minimized: bool = Field(default=False)
    has_activity: bool = Field(default=False)  # mirror of ligand_activities rows

    # Global status
    excluded: bool = Field(default=False, index=True)
    exclusion_reason: str = Field(default="")
    in_set: bool = Field(default=False)
    usage_class: str = Field(default=MoleculeUsageClass.GENERAL, index=True)

    primary_context: str = Field(default="")
    extra_data: dict = Field(default_factory=dict, sa_type=JSON)

    @field_validator("extra_data", mode="before")
    @classmethod
    def _coerce_extra_data(cls, value: Any) -> dict[str, Any]:
        return sanitize_molecule_extra_data(value)

    @field_validator("pains_matches", "ro5_violations", mode="before")
    @classmethod
    def _coerce_string_lists(cls, value: Any) -> list[str]:
        return _coerce_string_list(value)

    @classmethod
    def build_row(
            cls,
            *,
            project_root: Path,
            source_file: Path,
            source_index: int,
            name: str,
            molecule_type: str,
            n_atoms: int,
            input_format: str,
            stored_path: Path,
            current_path: Path | None = None,
            current_model_index: int | None = None,
            extra_data: dict | None = None,
            created_at: datetime | None = None,
            primary_context: str = "",
            usage_class: str = MoleculeUsageClass.GENERAL,
    ) -> dict[str, Any]:
        now = created_at or datetime.now()
        raw_meta = dict(extra_data or {}) if isinstance(extra_data, dict) else {}
        state = raw_meta.get("state", {}) if isinstance(raw_meta.get("state"), dict) else {}
        meta = sanitize_molecule_extra_data(extra_data)
        return {
            "name": str(name or f"{source_file.stem}_{source_index}"),
            "molecule_type": str(molecule_type or MoleculeType.UNKNOWN),
            "source": str(source_file.resolve()),
            "source_index": max(0, source_index),
            "input_format": str(input_format or FileFormat.UNKNOWN),
            "stored_path": str(stored_path.relative_to(project_root)),
            "current_path": str((current_path or stored_path).relative_to(project_root)),
            "current_model_index": None if current_model_index is None else int(current_model_index),
            "active_binding_site_id": None,
            "n_atoms": max(0, n_atoms),
            "mw": meta.get("mw"),
            "exact_mw": None,
            "logp": None,
            "hbd": None,
            "hba": None,
            "tpsa": None,
            "rotatable_bonds": None,
            "fragment_count": None,
            "ring_count": None,
            "aromatic_ring_count": None,
            "hetero_atom_count": None,
            "heavy_atom_count": None,
            "formal_charge": None,
            "fraction_csp3": None,
            "pains_matches": [],
            "ro5_violations": [],
            "conformer_count": max(0, int(state.get("conformer_count", 0) or 0)),
            "has_3d": bool(state.get("has_3d", False)),
            "has_hs": bool(state.get("has_hs", False)),
            "is_minimized": False,
            "has_activity": False,
            "excluded": False,
            "exclusion_reason": "",
            "in_set": False,
            "usage_class": str(usage_class or MoleculeUsageClass.GENERAL),
            "primary_context": primary_context,
            "extra_data": meta,
            "created_at": now,
            "updated_at": now,
        }


# ---------------------------------------------------------------------------
# MoleculeRepresentation
# ---------------------------------------------------------------------------

class MoleculeRepresentation(SQLModel, table=True):
    """
    1-D string representations. One row per (molecule, repr_type).
    Supports multiple types and edge cases (e.g. peptide with both
    sequence_aa and smiles_canonical).
    """

    __tablename__ = TABLE_MOLECULE_REPRESENTATIONS
    __table_args__ = (
        UniqueConstraint("molecule_id", "repr_type"),
        Index("idx_repr_lookup", "repr_type", "value"),
    )

    id: str = Field(primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    repr_type: str = Field()
    value: str = Field()

    @classmethod
    def build_rows(
            cls,
            molecule_id: int,
            representations: dict[str, str],
    ) -> list[dict[str, Any]]:
        return [
            {"molecule_id": molecule_id, "repr_type": rtype, "value": value}
            for rtype, value in representations.items()
            if value
        ]

    @classmethod
    def find_duplicate(cls, inchikey: str, session) -> int | None:
        from sqlmodel import select
        row = session.exec(
            select(cls)
            .where(cls.repr_type == ReprType.INCHI_KEY)
            .where(cls.value == inchikey)
        ).first()
        return row.molecule_id if row else None


class MoleculeSourceProperty(SQLModel, table=True):
    __tablename__ = TABLE_MOLECULE_SOURCE_PROPERTIES
    __table_args__ = (
        UniqueConstraint("molecule_id", "key"),
        Index("idx_source_prop_lookup", "key", "value_text"),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    key: str = Field(index=True)
    value_text: str = Field(default="")

    @classmethod
    def build_rows(
            cls,
            molecule_id: int,
            properties: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, value in dict(properties or {}).items():
            normalized_key = str(key or "").strip()
            normalized_value = str(value or "").strip()
            if not normalized_key or not normalized_value:
                continue
            rows.append(
                {
                    "molecule_id": int(molecule_id),
                    "key": normalized_key,
                    "value_text": normalized_value,
                }
            )
        return rows


# ---------------------------------------------------------------------------
# MoleculeModel  (conformers / structural models)
# ---------------------------------------------------------------------------

class MoleculeModel(SQLModel, table=True):
    """
    Structural models for a molecule. Index 0 is always the canonical model
    (imported or first generated). Additional indices are conformers or
    alternative models.

    molecules.stored_path always mirrors the canonical imported copy.
    molecules.current_path / current_model_index point to the active model.

    Applies to both small molecules (RDKit conformers) and proteins
    (ESMFold predictions, NMR ensembles).
    """

    __tablename__ = TABLE_MOLECULE_MODELS
    __table_args__ = (
        UniqueConstraint("molecule_id", "model_index"),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    model_index: int = Field(default=0)  # 0 = canonical
    file_path: str = Field(default="")  # relative to project root
    energy: float | None = Field(default=None)  # kcal/mol if minimized
    source: str = Field(default=ModelSource.IMPORTED)  # see ModelSource
    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
            cls,
            *,
            molecule_id: int,
            model_index: int,
            file_path: str,
            source: str = ModelSource.IMPORTED,
            energy: float | None = None,
    ) -> dict[str, Any]:
        return {
            "molecule_id": molecule_id,
            "model_index": model_index,
            "file_path": file_path,
            "energy": energy,
            "source": source,
            "created_at": datetime.now(),
        }
