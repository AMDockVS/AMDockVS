from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import JSON, UniqueConstraint, Index
from sqlmodel import SQLModel, Field

from amdockvs.constants import (
    TABLE_LIGAND_ACTIVITIES,
    TABLE_MOLECULES,
    TABLE_QSAR_MODELS,
    TABLE_QSAR_PREDICTIONS,
)


# ---------------------------------------------------------------------------
# LigandActivity
# ---------------------------------------------------------------------------

class LigandActivity(SQLModel, table=True):
    """
    Biological activity for a ligand. Three entry paths:
        1. Bulk import: file with smiles + activity columns
        2. ID-based:    existing molecule_id + value
        3. Manual:      UI entry per molecule

    has_activity on MoleculeRecord mirrors existence of rows here.
    That flag is maintained by the repository layer, not application callers.
    """

    __tablename__ = TABLE_LIGAND_ACTIVITIES

    id: int | None   = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)

    value: float
    unit: str           = Field(default="")   # nM, uM, ...
    activity_type: str  = Field(default="")   # IC50, Ki, Kd, pIC50, ...
    kind: str           = Field(default="continuous")  # "continuous" | "categorical" — picks the model task
    description: str    = Field(default="")
    source: str         = Field(default="")   # reference, assay id, etc.

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_row(
        cls,
        *,
        molecule_id: int,
        value: float,
        unit: str = "",
        activity_type: str = "",
        kind: str = "continuous",
        description: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        return {
            "molecule_id":   molecule_id,
            "value":         value,
            "unit":          unit,
            "activity_type": activity_type,
            "kind":          kind,
            "description":   description,
            "source":        source,
            "created_at":    datetime.now(),
        }


# ---------------------------------------------------------------------------
# QSARModel
# ---------------------------------------------------------------------------

class QSARModel(SQLModel, table=True):
    """
    A trained or externally loaded QSAR model.

    source:
        "trained"   — trained within AMDock using ligand_activities
        "external"  — loaded from file (pre-trained model)

    metrics JSON example:
        {"r2": 0.91, "rmse": 0.34, "q2": 0.88, "n_train": 312, "n_test": 78}

    feature_type describes what molecular representation was used:
        "fingerprints_ecfp4", "descriptors_2d", "descriptors_3d", ...
    """

    __tablename__ = TABLE_QSAR_MODELS

    id: int | None    = Field(default=None, primary_key=True)
    name: str         = Field(index=True)
    algorithm: str    = Field(default="")   # "rf" | "xgb" | "dnn" | "svr" | ...
    target: str       = Field(default="")   # "IC50" | "Ki" | "pIC50" | ...
    feature_type: str = Field(default="")
    source: str       = Field(default="trained")  # "trained" | "external"

    model_path: str   = Field(default="")   # serialized model file (relative)
    metrics: dict     = Field(default_factory=dict, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# QSARPrediction
# ---------------------------------------------------------------------------

class QSARPrediction(SQLModel, table=True):
    """
    Predicted activity value for a molecule using a specific QSAR model.
    One row per (molecule, model).

    confidence is model-dependent:
        RF/XGB → prediction std across trees
        DNN    → dropout-based uncertainty
        None   → model does not provide uncertainty estimates
    """

    __tablename__ = TABLE_QSAR_PREDICTIONS
    __table_args__ = (
        UniqueConstraint("molecule_id", "model_id"),
    )

    id: int | None   = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    model_id: int    = Field(foreign_key=f"{TABLE_QSAR_MODELS}.id", index=True)

    value: float
    confidence: float | None = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.now)

    @classmethod
    def build_rows(
        cls,
        model_id: int,
        predictions: list[dict],  # [{"molecule_id": int, "value": float, "confidence": float|None}]
    ) -> list[dict[str, Any]]:
        now = datetime.now()
        return [
            {
                "molecule_id": p["molecule_id"],
                "model_id":    model_id,
                "value":       p["value"],
                "confidence":  p.get("confidence"),
                "created_at":  now,
            }
            for p in predictions
        ]
