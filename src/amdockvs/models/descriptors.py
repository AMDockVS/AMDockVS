from __future__ import annotations

from datetime import datetime
from typing import List

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, JSON

from amdockvs.constants import (
    TABLE_DESCRIPTOR_BLOCKS,
    TABLE_DESCRIPTOR_SCHEMAS,
    TABLE_DESCRIPTOR_VECTORS,
    TABLE_FINGERPRINTS,
    TABLE_MOLECULES,
)
# Centralised vocabulary - re-exported so existing imports keep working.
from amdockvs.vocab import FingerprintType


class DescriptorBlockRecord(SQLModel, table=True):
    """A named block of descriptors for one molecule, stored as an inspectable JSON dict
    {name: value}. Referenceable by (molecule_id, block) — 'basic', 'rdkit2d', 'mordred'.
    ~5 KB/molecule for the 200-wide RDKit block, so it lives in the DB rather than a sidecar."""

    __tablename__ = TABLE_DESCRIPTOR_BLOCKS
    __table_args__ = (UniqueConstraint("molecule_id", "block"),)

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    block: str = Field(index=True)
    values_json: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: datetime = Field(default_factory=datetime.now)



class DescriptorSchema(SQLModel, table=True):
    __tablename__ = TABLE_DESCRIPTOR_SCHEMAS

    id: int | None = Field(default=None, primary_key=True)
    model_name: str = Field(index=True)  # ej. "rdkit_2d", "mordred"

    # Ordered list of the keys: ["MW", "LogP", "HBD", "HBA", ...]
    # Being ordered, the position defines the index into the binary array
    keys_order: List[str] = Field(sa_type=JSON)
    created_at: datetime = Field(default_factory=datetime.now)


class DescriptorVectorRecord(SQLModel, table=True):
    __tablename__ = TABLE_DESCRIPTOR_VECTORS
    __table_args__ = (
        UniqueConstraint("molecule_id", "schema_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    schema_id: int = Field(foreign_key=f"{TABLE_DESCRIPTOR_SCHEMAS}.id", index=True)

    # Values are stored as raw bytes (BLOB) using C floats
    values_binary: bytes = Field()


class FingerprintRecord(SQLModel, table=True):
    __tablename__ = TABLE_FINGERPRINTS
    __table_args__ = (
        # Prevents duplicating the same fingerprint type for the same molecule
        UniqueConstraint("molecule_id", "fp_type", "nbits", "radius"),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)

    # Flat metadata embedded inline (they take up negligible bytes)
    fp_type: str = Field(index=True)  # e.g. FingerprintType
    nbits: int = Field(default=2048)  # e.g. 1024, 2048
    radius: int | None = Field(default=None)  # e.g. 2, 3 (or None for MACCS)

    # The raw binary vector (BLOB)
    fp_binary: bytes = Field()

    created_at: datetime = Field(default_factory=datetime.now)
