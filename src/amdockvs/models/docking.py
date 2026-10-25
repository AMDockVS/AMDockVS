from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, UniqueConstraint, text
from sqlmodel import SQLModel, Field

from amdockvs.models.codes import code_field

from amdockvs.core.rows import binding_site_row, interaction_rows
from amdockvs.core.constants import (
    TABLE_PAYLOADS,
    TABLE_BINDING_SITES,
    TABLE_CONSENSUS_SCORES,
    TABLE_DOCKING_RESULTS,
    TABLE_DOCKING_RUNS,
    TABLE_LIGAND_PREPS,
    TABLE_RECEPTOR_PREPS,
    TABLE_MOLECULES,
    TABLE_INTERACTION_RESULTS,
)
from amdockvs.core.vocab import BindingSiteSource


# ---------------------------------------------------------------------------
# BindingSite
# ---------------------------------------------------------------------------

class BindingSite(SQLModel, table=True):
    """
    A binding site / docking grid definition for a molecule in receptor role.
    One receptor-capable molecule can have N binding sites from different sources.
    The active one has `is_active` set: at most one per molecule (a partial UNIQUE index).

    Sites are only ever added; nothing rewrites or renumbers them. A prediction re-run appends a
    new batch next to the old one and it is the user who deletes what they no longer want, so a
    site's `id` is stable for as long as it exists and is the only handle anyone needs.

    source_ref stores context depending on source:
        "ligand"  → molecule_id of the reference ligand (center of mass used)
        "fpocket" → pocket rank as string ("1", "2", ...)
        "p2rank"  → P2Rank pocket rank as string ("1", "2", ...)
        "pdb"     → PDB site annotation id
        "manual"  → empty
    """

    __tablename__ = TABLE_BINDING_SITES
    __table_args__ = (
        Index("uq_binding_site_active", "molecule_id", unique=True, sqlite_where=text("is_active=1")),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    is_active: bool = Field(default=False)

    name: str = Field(default="")
    source: str = code_field("binding_site_source", BindingSiteSource.MANUAL)
    source_ref: str = Field(default="")

    center_x: float | None = Field(default=None)
    center_y: float | None = Field(default=None)
    center_z: float | None = Field(default=None)
    size_x: float | None = Field(default=None)
    size_y: float | None = Field(default=None)
    size_z: float | None = Field(default=None)

    extra_data: dict | None = Field(default=None, sa_type=JSON)


    @property
    def is_defined(self) -> bool:
        return all(
            v is not None for v in (
                self.center_x, self.center_y, self.center_z,
                self.size_x, self.size_y, self.size_z,
            )
        )

    build_row = staticmethod(binding_site_row)


class ReceptorPrep(SQLModel, table=True):
    """A receptor prepared for one family (`ad4` serves Vina, QuickVina, gnina and AutoDock-GPU).

    Ready = `rigid_payload_id` set; a failed preparation leaves it NULL with `error`.
    `flex_residues` is the user's pick of flexible side chains, which a re-preparation keeps
    (it writes every column but this one); `flex_payload_id` is what was prepared from it.
    """

    __tablename__ = TABLE_RECEPTOR_PREPS
    __table_args__ = (UniqueConstraint("receptor_molecule_id", "family"),)

    id: int | None = Field(default=None, primary_key=True)
    receptor_molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id")
    family: str = code_field("prep_family", "ad4")
    rigid_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    flex_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    flex_residues: list = Field(default_factory=list, sa_type=JSON)
    error: str | None = Field(default=None)


class LigandPrep(SQLModel, table=True):
    """A ligand prepared for one family. Ready = `payload_id` set; NULL = failed, with `error`."""

    __tablename__ = TABLE_LIGAND_PREPS
    __table_args__ = (
        # "Which molecules are ready for family X" without a table scan; the PK rides along.
        Index("idx_ligand_prep_ready", "family", "payload_id"),
        {"sqlite_with_rowid": False},
    )

    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", primary_key=True)
    family: str = code_field("prep_family", "ad4", primary_key=True)
    payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    error: str | None = Field(default=None)


def prep_table(role_type: str):
    """`(model, molecule column, prepared payload column)` of a role's preparations."""
    if str(role_type).strip().lower() == "receptor":
        return ReceptorPrep, ReceptorPrep.receptor_molecule_id, ReceptorPrep.rigid_payload_id
    return LigandPrep, LigandPrep.molecule_id, LigandPrep.payload_id


# ---------------------------------------------------------------------------
# DockingResult
# ---------------------------------------------------------------------------

class DockingRun(SQLModel, table=True):
    """What every pose of one docking job shares: the engine, the run kind and the protocol.

    One row per (submit run_id, engine, run kind, protocol): a submit with two protocols starts two jobs
    under the same run_id. The job's parent creates it before the first chunk (`docking_run_id`)
    and each result row points to it, so none of this repeats per pose.
    """

    __tablename__ = TABLE_DOCKING_RUNS
    __table_args__ = (UniqueConstraint("run_id", "engine", "run_kind", "protocol_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(default="")             # the submit's id; "" for runs submitted without one
    engine: str = code_field("engine")
    run_kind: str = code_field("run_kind", "screening")
    protocol_hash: str = Field(default="", index=True)
    protocol_label: str = Field(default="")
    protocol: dict = Field(default_factory=dict, sa_type=JSON)  # the whole protocol: program, config
    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


class DockingResult(SQLModel, table=True):
    """
    One row per pose of a (receptor, ligand) pair in a docking run (`docking_run_id`).

    pose_rank=1 is the best pose. A failed pair is one row with score NULL and `error`.
    rmsd_vs_reference is filled in a redocking, against the complex's reference ligand.
    The ligand's own properties (MW, cLogP...) are in `molecules`; what derives from the score
    is stored as columns so the results view sorts and filters on them.
    """

    __tablename__ = TABLE_DOCKING_RESULTS
    __table_args__ = (
        Index("idx_dr_pair", "receptor_molecule_id", "ligand_molecule_id", "docking_run_id", "pose_rank"),
    )

    id: int | None    = Field(default=None, primary_key=True)
    docking_run_id: int = Field(foreign_key=f"{TABLE_DOCKING_RUNS}.id", index=True)
    receptor_molecule_id: int  = Field(foreign_key=f"{TABLE_MOLECULES}.id")  # idx_dr_pair leads with it
    ligand_molecule_id: int    = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    complex_id: int | None     = Field(default=None)  # the complex a redocking pair comes from
    pose_rank: int    = Field(default=1) # 1 = best pose

    score: float | None       = Field(default=None)   # binding energy kcal/mol
    score_type: str           = code_field("score_type")
    rmsd_vs_reference: float | None = Field(default=None)

    # Pose (SDF) as a payload of the "poses" series; the poses of a pair share one needle.
    pose_payload_id: int | None = Field(default=None, foreign_key=f"{TABLE_PAYLOADS}.id", index=True)
    # The receptor structure the pose was docked into. ponytail: no FK, a re-prepared receptor may drop it.
    receptor_payload_id: int | None = Field(default=None)

    # From the score and the ligand (`docking_metrics`); Ki is 10^-pKi.
    ligand_efficiency: float | None = Field(default=None)
    predicted_pki: float | None = Field(default=None)
    lle: float | None = Field(default=None)
    bei: float | None = Field(default=None)
    sei: float | None = Field(default=None)
    fit_quality: float | None = Field(default=None)

    error: str | None = Field(default=None)  # only on a failed pair
    # What only one engine reports: gnina's CNN values, AutoDock-GPU's energy terms, the RMSD method.
    extra: dict | None = Field(default=None, sa_type=JSON)

    @classmethod
    def build_row(
        cls,
        *,
        docking_run_id: int,
        receptor_molecule_id: int,
        ligand_molecule_id: int,
        pose_rank: int = 1,
        score: float | None = None,
        score_type: str = "",
        pose_payload_id: int | None = None,
        **columns: Any,
    ) -> dict[str, Any]:
        return {
            "docking_run_id": docking_run_id,
            "receptor_molecule_id": receptor_molecule_id,
            "ligand_molecule_id": ligand_molecule_id,
            "pose_rank": pose_rank,
            "score": score,
            "score_type": score_type,
            "pose_payload_id": pose_payload_id,
            **{key: value for key, value in columns.items() if key in cls.model_fields},
        }


# ---------------------------------------------------------------------------
# ConsensusScore
# ---------------------------------------------------------------------------

class ConsensusScore(SQLModel, table=True):
    """
    Combined score across N engines for the same receptor-ligand molecule pair.
    Computed as a post-processing step when docking_results exist for
    >= 2 engines.

    method examples:
        "rank_average"   — average of per-engine pose ranks
        "score_zscore"   — z-score normalized score combination
        "ecr"            — exponential consensus ranking
    """

    __tablename__ = TABLE_CONSENSUS_SCORES
    __table_args__ = (
        UniqueConstraint("receptor_molecule_id", "ligand_molecule_id", "method"),
    )

    id: int | None   = Field(default=None, primary_key=True)
    receptor_molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    ligand_molecule_id: int   = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)

    method: str      = Field()
    score: float     = Field()
    engines_used: list = Field(default_factory=list, sa_type=JSON)  # ["vina","adgpu"]

    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)

# ---------------------------------------------------------------------------
# InteractionsResult
# ---------------------------------------------------------------------------

class InteractionsResult(SQLModel, table=True):
    """
    Protein-ligand interaction detected by ms_contactmap.
    One row per interaction (a single pose typically has many).

    interaction_type examples:
        "hydrophobic", "hydrogen_bond", "salt_bridge",
        "water_bridge", "pi_stacking", "pi_cation", "halogen_bond"

    residue format: "ALA123:A" (resname + resnum + chain)

    Keyed by the structures, not by a docking result: the receptor the pose sits in and the
    pose itself, so a reference complex has interactions too. A pose with none detected keeps
    one "none" row: computed, nothing found.
    """

    __tablename__ = TABLE_INTERACTION_RESULTS
    __table_args__ = (
        Index("idx_interaction_pose", "pose_payload_id", "receptor_payload_id"),
    )

    id: int | None           = Field(default=None, primary_key=True)
    receptor_payload_id: int = Field()  # ponytail: no FK, like docking_results: a re-prepared receptor may drop it
    pose_payload_id: int     = Field(foreign_key=f"{TABLE_PAYLOADS}.id")

    interaction_type: str  = Field()
    residue: str           = Field()         # "ALA123:A"
    residue_index: int     = Field(default=0)
    distance: float | None = Field(default=None)

    # Additional geometry stored as JSON — varies by interaction type
    # hydrogen_bond: {"donor_angle": ..., "acceptor_angle": ...}
    # pi_stacking:   {"angle": ..., "offset": ...}
    geometry: dict = Field(default_factory=dict, sa_type=JSON)


    build_rows = staticmethod(interaction_rows)
