from datetime import datetime
from typing import Any

from sqlalchemy import Index, JSON, UniqueConstraint
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_BINDING_SITES,
    TABLE_CONSENSUS_SCORES,
    TABLE_DOCKING_RESULTS,
    TABLE_ENGINES,
    TABLE_MOLECULES,
    TABLE_INTERACTION_RESULTS,
)
from amdockvs.vocab import BindingSiteSource


# ---------------------------------------------------------------------------
# BindingSite
# ---------------------------------------------------------------------------

class BindingSite(SQLModel, table=True):
    """
    A binding site / docking grid definition for a molecule in receptor role.
    One receptor-capable molecule can have N binding sites from different sources.
    The active one is referenced by MoleculeRecord.active_binding_site_id.

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

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)

    name: str = Field(default="")
    source: str = Field(default=BindingSiteSource.MANUAL)  # ver BindingSiteSource
    source_ref: str = Field(default="")

    center_x: float | None = Field(default=None)
    center_y: float | None = Field(default=None)
    center_z: float | None = Field(default=None)
    size_x: float | None = Field(default=None)
    size_y: float | None = Field(default=None)
    size_z: float | None = Field(default=None)

    extra_data: dict | None = Field(default=None, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def is_defined(self) -> bool:
        return all(
            v is not None for v in (
                self.center_x, self.center_y, self.center_z,
                self.size_x, self.size_y, self.size_z,
            )
        )

    @classmethod
    def build_row(
            cls,
            *,
            molecule_id: int,
            name: str = "",
            source: str = BindingSiteSource.MANUAL,
            source_ref: str = "",
            center: tuple[float, float, float] | None = None,
            size: tuple[float, float, float] | None = None,
            extra_data: dict | None = None,
    ) -> dict[str, Any]:
        # A row heading to a sink is written as-is: SQLModel does not run here, so the
        # default_factory of created_at (NOT NULL) never fires. build_row sets it.
        cx, cy, cz = center or (None, None, None)
        sx, sy, sz = size or (None, None, None)
        return {
            "molecule_id": molecule_id,
            "name": name,
            "source": source,
            "source_ref": source_ref,
            "center_x": cx, "center_y": cy, "center_z": cz,
            "size_x": sx, "size_y": sy, "size_z": sz,
            "extra_data": extra_data,
            "created_at": datetime.now(),
        }


class EngineState(SQLModel, table=True):
    """
    Engine-specific preparation state for a molecule acting in a given role.
    One row per (molecule_id, role_type, engine).

    role_type: "receptor" | "ligand"
    molecule_id: molecules.id

    files JSON structure varies by engine:
        vina:      {"prepared": "path/to/receptor.pdbqt"}
        autodock4: {"prepared": "path/to/receptor.pdbqt",
                    "maps":     "path/to/maps/"}
        diffdock:  {}   (uses sequence/smiles directly — no prep files)
        dock6:     {"mol2": "path/to/receptor.mol2",
                    "spheres": "path/to/receptor.sph"}

    is_ready=True means all required files for this engine exist and are valid.
    Pre-flight checks query: WHERE molecule_id=X AND role_type=Y AND engine=Z AND is_ready=True
    """

    __tablename__ = TABLE_ENGINES
    __table_args__ = (
        UniqueConstraint("molecule_id", "role_type", "engine"),
        Index("idx_engine_state_lookup", "molecule_id", "role_type", "engine"),
        # Covering index for the other direction: "how many / which molecules are ready for
        # engine X". Without it that scan is ~3.5x slower (270ms vs 79ms over 900k rows).
        Index("idx_engine_state_role_engine", "role_type", "engine", "is_ready"),
    )

    id: int | None = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    role_type: str = Field(index=True)  # "receptor" | "ligand"
    engine: str = Field(index=True)  # "vina" | "autodock4" | ...

    files: dict = Field(default_factory=dict, sa_type=JSON)
    is_ready: bool = Field(default=False, index=True)

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
            cls,
            *,
            molecule_id: int,
            role_type: str,
            engine: str,
            files: dict | None = None,
            is_ready: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now()
        return {
            "molecule_id": molecule_id,
            "role_type": role_type,
            "engine": engine,
            "files": files or {},
            "is_ready": is_ready,
            "created_at": now,
            "updated_at": now,
        }


# ---------------------------------------------------------------------------
# DockingResult
# ---------------------------------------------------------------------------

class DockingResult(SQLModel, table=True):
    """
    One row per (receptor molecule, ligand molecule, engine, pose).
    A single docking run produces N poses — each is a separate row.

    pose_rank=1 is the best pose. Downstream analysis (interactions, consensus)
    references individual rows by id.

    rmsd_vs_reference is populated only for redocking experiments
    (set purpose='redocking') after comparing against crystal_pose_path.
    """

    __tablename__ = TABLE_DOCKING_RESULTS
    __table_args__ = (
        Index("idx_dr_receptor_molecule",  "receptor_molecule_id"),
        Index("idx_dr_ligand_molecule",    "ligand_molecule_id"),
        Index("idx_dr_engine",    "engine"),
        Index(
            "idx_dr_rank",
            "receptor_molecule_id",
            "ligand_molecule_id",
            "engine",
            "pose_rank",
        ),
    )

    id: int | None    = Field(default=None, primary_key=True)
    receptor_molecule_id: int  = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    ligand_molecule_id: int    = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    engine: str       = Field()          # "vina" | "autodock4" | "diffdock" | ...
    pose_rank: int    = Field(default=1) # 1 = best pose

    # Scoring
    score: float | None       = Field(default=None)   # binding energy kcal/mol
    score_type: str           = Field(default="")     # "vina_score" | "ad4_score" | ...

    # Pose file — relative to project root
    pose_path: str            = Field(default="")

    # Redocking validation — populated by post-processing step
    rmsd_vs_reference: float | None = Field(default=None)

    # Additional engine-specific metrics stored as JSON
    # Vina:      {"gauss1": ..., "gauss2": ..., "repulsion": ..., ...}
    # AutoDock4: {"intermolecular": ..., "internal": ..., ...}
    # DiffDock:  {"confidence": ...}
    metrics: dict = Field(default_factory=dict, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
        cls,
        *,
        receptor_molecule_id: int,
        ligand_molecule_id: int,
        engine: str,
        pose_rank: int = 1,
        score: float | None = None,
        score_type: str = "",
        pose_path: str = "",
        metrics: dict | None = None,
    ) -> dict[str, Any]:
        return {
            "receptor_molecule_id": receptor_molecule_id,
            "ligand_molecule_id":   ligand_molecule_id,
            "engine":             engine,
            "pose_rank":          pose_rank,
            "score":              score,
            "score_type":         score_type,
            "pose_path":          pose_path,
            "rmsd_vs_reference":  None,
            "metrics":            metrics or {},
            "created_at":         datetime.now(),
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
    engines_used: list = Field(default_factory=list, sa_type=JSON)  # ["vina","autodock4"]

    created_at: datetime = Field(default_factory=datetime.now)

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
    """

    __tablename__ = TABLE_INTERACTION_RESULTS
    __table_args__ = (
        Index("idx_interaction_result", "docking_result_id"),
    )

    id: int | None         = Field(default=None, primary_key=True)
    docking_result_id: int = Field(foreign_key=f"{TABLE_DOCKING_RESULTS}.id", index=True)

    interaction_type: str  = Field()
    residue: str           = Field()         # "ALA123:A"
    residue_index: int     = Field(default=0)
    distance: float | None = Field(default=None)

    # Additional geometry stored as JSON — varies by interaction type
    # hydrogen_bond: {"donor_angle": ..., "acceptor_angle": ...}
    # pi_stacking:   {"angle": ..., "offset": ...}
    geometry: dict = Field(default_factory=dict, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_rows(
        cls,
        docking_result_id: int,
        interactions: list[dict],
    ) -> list[dict[str, Any]]:
        now = datetime.now()
        return [
            {
                "docking_result_id": docking_result_id,
                "interaction_type":  i["interaction_type"],
                "residue":           i["residue"],
                "residue_index":     i.get("residue_index", 0),
                "distance":          i.get("distance"),
                "geometry":          i.get("geometry", {}),
                "created_at":        now,
            }
            for i in interactions
        ]
