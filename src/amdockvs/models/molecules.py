from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Computed, DateTime, Index, Integer, JSON, UniqueConstraint, text
from pydantic import field_validator
from sqlmodel import SQLModel, Field

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_IMPORT_BATCHES,
    TABLE_MOLECULE_MODELS,
    TABLE_MOLECULE_REPRESENTATIONS,
    TABLE_MOLECULES,
    TABLE_PAYLOADS,
)
from amdockvs.core.rows import molecule_model_row, molecule_row, sanitize_molecule_extra_data

# Domain vocabulary centralised in amdockvs.core.vocab. Re-exported here so existing
# imports keep working (`from amdockvs.models.molecules import MoleculeType`).
from amdockvs.core.vocab import (
    FileFormat,
    ImportKind,
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


class ImportBatch(SQLModel, table=True):
    """One imported file: where its rows or shards came from, and how far the import got.

    `quick_hash` finds a file imported before; `n_records` (records read) against the file's
    record count tells a complete import from a partial one (`io.batches.previous_import`).
    """

    __tablename__ = TABLE_IMPORT_BATCHES

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(default="")  # download label or URL; the absolute path when local
    quick_hash: str = Field(default="", index=True)
    n_records: int = Field(default=0)
    n_imported: int = Field(default=0)
    format: str = code_field("file_format", FileFormat.UNKNOWN)
    kind: str = code_field("import_kind", ImportKind.ROWS)
    # ponytail: no FK — project_job_index lives in the master db, not in project.db.
    job_id: str = Field(default="")
    extra: dict = Field(default_factory=dict, sa_type=JSON)  # import options: context, role, ...
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


# ---------------------------------------------------------------------------
# MoleculeRecord
# ---------------------------------------------------------------------------

class MoleculeRecord(SQLModel, table=True):
    """
    Central registry. One row per molecule regardless of role.

    Status logic:
        excluded=False, usage_class=general   → active general pool
        excluded=False, usage_class=reference → reference-only molecules (e.g. cocrystals)
        excluded=True                         → globally discarded

    Representations live in molecule_representations, identity included (inchikey,
    smiles_canonical: finds duplicates and activities).
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
    molecule_type: str = code_field("molecule_type", MoleculeType.UNKNOWN)
    import_batch_id: int | None = Field(default=None, foreign_key=f"{TABLE_IMPORT_BATCHES}.id")
    source_index: int = Field(default=0)  # position in the batch's file

    # Roles
    is_receptor: bool = Field(default=False)
    is_ligand: bool = Field(default=False)

    # Payloads (ms_flow volumes): the bytes live in volumes/, the format is the payload's kind.
    # stored → the input as imported
    # current → the working structure (selected model/conformer, protonated, 3D...); NULL while
    # it is the stored one. Read either through `core.payloads.molecule_payload_id`; the model
    # holding that payload is the active one.
    stored_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")
    current_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")
    # properties → the import batch's SDF tags (parquet, shared by the batch), read by source_index
    properties_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id")

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
    # Lipinski violations: arithmetic on mw/logp/hbd/hba (`chemistry.filtering._ro5_violations`),
    # so SQLite computes it on read and stores nothing. Never written.
    ro5_violations: int | None = Field(default=None, sa_column=Column(Integer, Computed(
        "(coalesce(mw, 0) > 500) + (coalesce(logp, 0) > 5) + (coalesce(hbd, 0) > 5) + (coalesce(hba, 0) > 10)",
        persisted=False)))
    conformer_count: int = Field(default=0)

    # Fast-check state flags — updated by the layer that performs each operation.
    # Avoid JOINs for pre-flight checks before launching batch jobs.
    has_3d: bool = Field(default=False)
    has_hs: bool = Field(default=False)
    is_minimized: bool = Field(default=False)

    # Global status
    excluded: bool = Field(default=False)
    exclusion_reason: str = Field(default="")
    in_set: bool = Field(default=False)
    usage_class: str = code_field("usage_class", MoleculeUsageClass.GENERAL)

    extra_data: dict = Field(default_factory=dict, sa_type=JSON)

    @field_validator("extra_data", mode="before")
    @classmethod
    def _coerce_extra_data(cls, value: Any) -> dict[str, Any]:
        return sanitize_molecule_extra_data(value)

    @field_validator("pains_matches", mode="before")
    @classmethod
    def _coerce_pains(cls, value: Any) -> list[str] | None:
        return None if value is None or value == "null" else _coerce_string_list(value)

    build_row = staticmethod(molecule_row)


# Indexes of `molecules` that a bulk import builds once at its end: kept live, a random key
# (a name) turns its appends into random page writes, and the rate falls with the table size.
# ms_flow builds them (`ProjectStore.ensure_indexes`) when an import ends and when a project
# opens, so a project whose import died gets them back. A usage_class index is not partial: SQLite only
# uses a partial index whose WHERE terms the query repeats, and `usage_class = 'reference'`
# does not repeat `usage_class != 'general'`.
#
# With no table stats SQLite takes the index matching the most `=` terms, then the one that
# needs no sort. So a page (ORDER BY id) of any role filter walks ix_molecules_usage_class in
# id order and stops at the page; a ligand count matches all four terms of
# idx_mol_role_scope and reads only that index (~8x fewer pages than the table); a receptor
# query takes idx_mol_receptor (or idx_mol_receptor_id), which ties with usage_class and holds
# only the receptors — without it, finding a handful of receptors walks millions of ligands.
DEFERRED_INDEXES = {
    "ix_molecules_usage_class": "(usage_class)",
    "idx_mol_batch": "(import_batch_id, source_index)",
    "idx_mol_active": "(id) WHERE excluded=0 AND in_set=0",
    "idx_mol_role_scope": "(usage_class, is_ligand, excluded, molecule_type)",
    "idx_mol_receptor": "(usage_class) WHERE is_receptor=1",
    "idx_mol_receptor_id": "(id) WHERE is_receptor=1",  # no usage_class term
    # PayloadStore.release/delete look a payload's referrers up by these (`col = id` implies
    # NOT NULL, so a partial index serves it). current/properties are NULL on most rows.
    "ix_molecules_stored_payload_id": "(stored_payload_id)",
    "idx_mol_current_payload": "(current_payload_id) WHERE current_payload_id IS NOT NULL",
    "idx_mol_properties_payload": "(properties_payload_id) WHERE properties_payload_id IS NOT NULL",
}
# No query needs them: no lookup by InChIKey, `excluded` is covered by the partial indexes, a
# name search or sort scans (~0.5 s a page at 3.85M rows, run async); the payload ones were
# replaced by partial ones (`CREATE INDEX IF NOT EXISTS` never rebuilds a changed definition).
RETIRED_INDEXES = (
    "ix_molecules_inchikey",
    "ix_molecules_excluded",
    "ix_molecules_name",
    "ix_molecules_current_payload_id",
    "ix_molecules_properties_payload_id",
)


def ensure_molecule_indexes(project) -> None:
    """Build the deferred indexes that are missing and drop the retired ones (older projects)."""
    _store(project).ensure_indexes(TABLE_MOLECULES, DEFERRED_INDEXES, retired=RETIRED_INDEXES)


def molecule_indexes_ready(project) -> bool:
    """False while an import's deferred indexes are dropped or still building (uncommitted)."""
    return _store(project).indexes_ready(TABLE_MOLECULES, DEFERRED_INDEXES)


def drop_deferred_indexes(project) -> None:
    _store(project).drop_indexes(DEFERRED_INDEXES)


def _store(project):
    from ms_flow.core.database import resolve_project_store

    return resolve_project_store(project)


# ---------------------------------------------------------------------------
# MoleculeRepresentation
# ---------------------------------------------------------------------------

class MoleculeRepresentation(SQLModel, table=True):
    """
    1-D string representations. One row per (molecule, repr_type).
    Supports multiple types and edge cases (e.g. peptide with both
    sequence_aa and smiles_canonical). Identity of the current structure (small molecules):
    inchikey and smiles_canonical, isomeric without explicit Hs — charged atoms keep theirs in
    brackets.
    """

    __tablename__ = TABLE_MOLECULE_REPRESENTATIONS
    __table_args__ = (
        Index("idx_repr_lookup", "repr_type", "value"),
        {"sqlite_with_rowid": False},
    )

    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", primary_key=True)
    repr_type: str = code_field("repr_type", primary_key=True)
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


# ---------------------------------------------------------------------------
# MoleculeModel  (conformers / structural models)
# ---------------------------------------------------------------------------

class MoleculeModel(SQLModel, table=True):
    """
    Structural models for a molecule, in the order they were made (the id). A step that makes
    several of one molecule (conformers) writes them as one PayloadGroup, so they compress
    together like poses.

    The active model is the one whose payload is the molecule's structure
    (`core.payloads.molecule_payload_id`): no index on the molecule points at it.

    Applies to both small molecules (RDKit conformers) and proteins
    (ESMFold predictions, NMR ensembles).
    """

    __tablename__ = TABLE_MOLECULE_MODELS

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    source: str = code_field("model_source", ModelSource.IMPORTED)
    # Per-model data as a JSON payload (e.g. ESMFold plddt_mean, ptm, pae); NULL if none.
    data_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)

    build_row = staticmethod(molecule_model_row)
