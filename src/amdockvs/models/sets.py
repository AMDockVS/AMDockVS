from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import UniqueConstraint, JSON
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_MOLECULE_SET_MEMBERS,
    TABLE_MOLECULE_SETS,
    TABLE_MOLECULES,
)
# Centralised vocabulary - re-exported so existing imports keep working.
from amdockvs.vocab import SetPurpose


# ---------------------------------------------------------------------------
# MoleculeSet
# ---------------------------------------------------------------------------

class MoleculeSet(SQLModel, table=True):
    __tablename__ = TABLE_MOLECULE_SETS

    id: int | None = Field(default=None, primary_key=True)
    name: str      = Field(index=True)
    purpose: str   = Field(default=SetPurpose.CUSTOM)  # see SetPurpose
    description: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# MoleculeSetMember
# ---------------------------------------------------------------------------

class MoleculeSetMember(SQLModel, table=True):
    """
    Membership of a molecule in a set, plus role-specific context flags.
    A molecule can belong to multiple sets simultaneously.
    excluded=True on the molecule still applies inside sets.
    """

    __tablename__ = TABLE_MOLECULE_SET_MEMBERS
    __table_args__ = (
        UniqueConstraint("molecule_id", "set_id"),
    )

    id: int | None   = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    set_id: int      = Field(foreign_key=f"{TABLE_MOLECULE_SETS}.id", index=True)

    # Context flags — only relevant for specific set purposes
    is_grid_reference:    bool = Field(default=False)  # grid_reference sets
    crystal_pose_path:    str  = Field(default="")     # redocking sets
    use_as_pharmacophore: bool = Field(default=False)  # substructure sets
    use_as_substructure:  bool = Field(default=False)  # substructure sets

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
        cls,
        *,
        molecule_id: int,
        set_id: int,
        is_grid_reference: bool = False,
        crystal_pose_path: str = "",
        use_as_pharmacophore: bool = False,
        use_as_substructure: bool = False,
    ) -> dict[str, Any]:
        return {
            "molecule_id":          molecule_id,
            "set_id":               set_id,
            "is_grid_reference":    is_grid_reference,
            "crystal_pose_path":    crystal_pose_path,
            "use_as_pharmacophore": use_as_pharmacophore,
            "use_as_substructure":  use_as_substructure,
            "created_at":           datetime.now(),
        }


# Temporary compatibility aliases while consumers migrate to the new set schema.
SetRecord = MoleculeSet
SetItemRecord = MoleculeSetMember
