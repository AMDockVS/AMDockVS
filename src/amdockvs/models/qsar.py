from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import DateTime, JSON, UniqueConstraint, Index
from sqlmodel import SQLModel, Field

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
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

    A molecule has an activity when it has a row here (`has_activity` filters with a subquery).
    That flag is maintained by the repository layer, not application callers.
    """

    __tablename__ = TABLE_LIGAND_ACTIVITIES

    id: int | None   = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)

    value: float
    unit: str           = Field(default="")   # nM, uM, ...
    activity_type: str  = Field(default="")   # IC50, Ki, Kd, pIC50, ...
    kind: str           = code_field("activity_kind", "continuous")  # picks the model task
    description: str    = Field(default="")
    source: str         = Field(default="")   # reference, assay id, etc.


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
    algorithm: str    = code_field("qsar_algorithm")
    target: str       = Field(default="")   # "IC50" | "Ki" | "pIC50" | ...
    feature_type: str = code_field("feature_type")
    source: str       = Field(default="trained")  # "trained" | "external"

    model_path: str   = Field(default="")   # serialized model file (relative)
    metrics: dict     = Field(default_factory=dict, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


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


    @classmethod
    def build_rows(
        cls,
        model_id: int,
        predictions: list[dict],  # [{"molecule_id": int, "value": float, "confidence": float|None}]
    ) -> list[dict[str, Any]]:
        return [
            {
                "molecule_id": p["molecule_id"],
                "model_id":    model_id,
                "value":       p["value"],
                "confidence":  p.get("confidence"),
            }
            for p in predictions
        ]
