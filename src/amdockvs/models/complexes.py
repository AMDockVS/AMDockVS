from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, JSON, UniqueConstraint
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_BINDING_SITES,
    TABLE_COMPLEXES,
    TABLE_LIGAND_ACTIVITIES,
    TABLE_MOLECULES,
)
from amdockvs.vocab import ComplexPurpose


class ComplexRecord(SQLModel, table=True):
    """
    Explicit receptor-ligand pair used for redocking or rescoring.

    This is the operational link between:
    - the processed receptor molecule used operationally
    - a frozen receptor reference file captured at pair creation time
    - the extracted/reference ligand molecule
    - the selected binding site on the receptor
    """

    __tablename__ = TABLE_COMPLEXES
    __table_args__ = (
        UniqueConstraint("receptor_molecule_id", "ligand_molecule_id", "purpose"),
        Index("idx_complex_receptor", "receptor_molecule_id"),
        Index("idx_complex_ligand", "ligand_molecule_id"),
        Index("idx_complex_purpose", "purpose"),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(default="", index=True)

    receptor_molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    ligand_molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    reference_receptor_path: str = Field(default="")
    # Frozen copy of the native ligand pose captured at pair creation. RMSD for redocking
    # must measure against this snapshot, not the ligand's live current_path (which prep/3D
    # regen can move) — otherwise validation compares against a target that changed.
    reference_ligand_path: str = Field(default="")

    binding_site_id: int | None = Field(default=None, foreign_key=f"{TABLE_BINDING_SITES}.id")
    activity_id: int | None = Field(default=None, foreign_key=f"{TABLE_LIGAND_ACTIVITIES}.id")

    purpose: str = Field(default=ComplexPurpose.REDOCKING, index=True)
    metadata_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
