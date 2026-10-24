from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import DateTime, Index, JSON, UniqueConstraint, text
from pydantic import field_validator
from sqlmodel import SQLModel, Field

from amdockvs.core.constants import (
    TABLE_BINDING_SITES,
    TABLE_MOLECULE_MODELS,
    TABLE_MOLECULE_REPRESENTATIONS,
    TABLE_MOLECULE_SOURCE_PROPERTIES,
    TABLE_MOLECULES,
    TABLE_PAYLOADS,
)
from amdockvs.core.rows import molecule_model_row, molecule_row, sanitize_molecule_extra_data
from .base import TimestampedRecord

# Domain vocabulary centralised in amdockvs.core.vocab. Re-exported here so existing
# imports keep working (`from amdockvs.models.molecules import MoleculeType`).
from amdockvs.core.vocab import (
    FileFormat,
    ModelSource,
    MoleculeType,
    MoleculeUsageClass,
    ReprType,
)






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
    # The rest of its indexes are DEFERRED_INDEXES: built after a bulk import, not during it.
    __table_args__ = (
        Index("idx_mol_excluded", "id", sqlite_where=text("excluded=1")),
        Index("idx_mol_in_set", "id", sqlite_where=text("in_set=1")),
    )

    id: int | None = Field(default=None, primary_key=True)

    # Identity (immutable after ingestion)
    name: str = Field(default="")
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

    # Payloads (ms_flow volumes): the bytes live in volumes/, the format is the payload's kind.
    # stored → the input as imported, only when it differs from the current form (D11)
    # current → the working structure (selected model/conformer, protonated, 3D...)
    stored_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")
    current_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")
    current_model_index: int | None = Field(default=None)
    # properties → the import batch's SDF tags (parquet, shared by the batch), read by source_index
    properties_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")

    # Identity of the current structure (small molecules): finds duplicates and activities.
    # Canonical isomeric SMILES without explicit Hs — charged atoms keep theirs in brackets.
    inchikey: str | None = Field(default=None)
    canonical_smiles: str | None = Field(default=None)

    # Basic chemical properties
    n_atoms: int = Field(default=0)
    mw: float | None = Field(default=None)
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
    # None (JSON null) = not computed: a library import leaves it to the descriptor job.
    pains_matches: list[str] | None = Field(default_factory=list, sa_type=JSON)
    ro5_violations: list[str] = Field(default_factory=list, sa_type=JSON)
    conformer_count: int = Field(default=0)

    # Fast-check state flags — updated by the layer that performs each operation.
    # Avoid JOINs for pre-flight checks before launching batch jobs.
    has_3d: bool = Field(default=False)
    has_hs: bool = Field(default=False)
    is_minimized: bool = Field(default=False)
    has_activity: bool = Field(default=False)  # mirror of ligand_activities rows

    # Global status
    excluded: bool = Field(default=False)
    exclusion_reason: str = Field(default="")
    in_set: bool = Field(default=False)
    usage_class: str = Field(default=MoleculeUsageClass.GENERAL)

    primary_context: str = Field(default="")
    extra_data: dict = Field(default_factory=dict, sa_type=JSON)

    @field_validator("extra_data", mode="before")
    @classmethod
    def _coerce_extra_data(cls, value: Any) -> dict[str, Any]:
        return sanitize_molecule_extra_data(value)

    @field_validator("ro5_violations", mode="before")
    @classmethod
    def _coerce_string_lists(cls, value: Any) -> list[str]:
        return _coerce_string_list(value)

    @field_validator("pains_matches", mode="before")
    @classmethod
    def _coerce_pains(cls, value: Any) -> list[str] | None:
        return None if value is None or value == "null" else _coerce_string_list(value)

    build_row = staticmethod(molecule_row)


# Indexes of `molecules` that a bulk import builds once at its end: kept live, a random key
# (a name) turns its appends into random page writes, and the rate falls with the table size.
# Created by `ensure_molecule_indexes` when an import ends and when a project opens, so a
# project whose import died gets them back. A usage_class index is not partial: SQLite only
# uses a partial index whose WHERE terms the query repeats, and `usage_class = 'reference'`
# does not repeat `usage_class != 'general'`.
DEFERRED_INDEXES = {
    "ix_molecules_name": f"{TABLE_MOLECULES} (name)",
    "ix_molecules_usage_class": f"{TABLE_MOLECULES} (usage_class)",
    "idx_mol_active": f"{TABLE_MOLECULES} (id) WHERE excluded=0 AND in_set=0",
    # PayloadStore.release/delete look a payload's referrers up by these.
    "ix_molecules_stored_payload_id": f"{TABLE_MOLECULES} (stored_payload_id)",
    "ix_molecules_current_payload_id": f"{TABLE_MOLECULES} (current_payload_id)",
    "ix_molecules_properties_payload_id": f"{TABLE_MOLECULES} (properties_payload_id)",
}
# No query uses them: no lookup by InChIKey, and `excluded` is covered by the partial indexes.
RETIRED_INDEXES = ("ix_molecules_inchikey", "ix_molecules_excluded")


def ensure_molecule_indexes(engine) -> None:
    """Build the deferred indexes that are missing and drop the retired ones (older projects)."""
    with engine.begin() as conn:
        for name in RETIRED_INDEXES:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        for name, target in DEFERRED_INDEXES.items():
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")


def molecule_indexes_ready(engine) -> bool:
    """False while an import's deferred indexes are dropped or still building (uncommitted)."""
    from sqlalchemy import inspect

    return DEFERRED_INDEXES.keys() <= {ix["name"] for ix in inspect(engine).get_indexes(TABLE_MOLECULES)}


def drop_deferred_indexes(engine) -> None:
    with engine.begin() as conn:
        for name in DEFERRED_INDEXES:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")


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

    molecules.current_payload_id / current_model_index point to the active model.

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
    payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    energy: float | None = Field(default=None)  # kcal/mol if minimized
    source: str = Field(default=ModelSource.IMPORTED)  # see ModelSource
    # Per-model quality data (e.g. ESMFold plddt_mean/ptm); ESMFold's PAE matrix as a payload.
    metrics: dict = Field(default_factory=dict, sa_type=JSON)
    pae_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)

    build_row = staticmethod(molecule_model_row)
