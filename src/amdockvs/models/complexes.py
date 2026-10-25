from __future__ import annotations


from sqlalchemy import Index, JSON, UniqueConstraint
from sqlmodel import SQLModel, Field

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_PAYLOADS,
    TABLE_BINDING_SITES,
    TABLE_COMPLEXES,
    TABLE_LIGAND_ACTIVITIES,
    TABLE_MOLECULES,
)
from amdockvs.core.vocab import ComplexPurpose


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
    reference_receptor_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    # Frozen copy of the native ligand pose captured at pair creation. RMSD for redocking
    # must measure against this snapshot, not the ligand's live current payload (which prep/3D
    # regen can replace) — otherwise validation compares against a target that changed.
    reference_ligand_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)

    binding_site_id: int | None = Field(default=None, foreign_key=f"{TABLE_BINDING_SITES}.id")
    activity_id: int | None = Field(default=None, foreign_key=f"{TABLE_LIGAND_ACTIVITIES}.id")

    purpose: str = code_field("complex_purpose", ComplexPurpose.REDOCKING, index=True)
    metadata_json: str = Field(default="{}")
