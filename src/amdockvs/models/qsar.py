from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import DateTime, JSON, UniqueConstraint, Index
from sqlmodel import SQLModel, Field

from amdockvs.models.codes import code_field

from amdockvs.core.constants import (
    TABLE_LIGAND_ACTIVITIES,
    TABLE_MOLECULES,
    TABLE_QSAR_MODEL_MEMBERS,
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
    A trained or externally loaded QSAR model: the registry row. The fitted model itself lives on
    disk at ``model_path`` (a file or a directory, whatever its ``engine`` writes).

    source:
        "trained"   — trained within AMDock using ligand_activities
        "external"  — loaded from file (pre-trained model)

    The headline scores are columns so models sort and filter in SQL; NULL when the model has no
    such score (a regression has no MCC, a model without holdout no test score). ``metrics`` keeps
    the rest: per-subset metric dicts, CV/y-scramble details, feature importance.

    feature_type describes what molecular representation was used:
        "fingerprints_ecfp4", "descriptors_2d", ...
    """

    __tablename__ = TABLE_QSAR_MODELS

    id: int | None    = Field(default=None, primary_key=True)
    name: str         = Field(index=True)
    algorithm: str    = code_field("qsar_algorithm")
    target: str       = Field(default="")   # "IC50" | "Ki" | "pIC50" | ...
    feature_type: str = code_field("feature_type")
    source: str       = Field(default="trained")  # "trained" | "external"
    task: str         = Field(default="regression")  # "regression" | "classification"
    engine: str       = Field(default="sklearn")  # who reads model_path
    splitter: str     = Field(default="")  # how train/test/folds were drawn ("" = external)

    n_train: int      = Field(default=0)
    n_test: int       = Field(default=0)
    test_r2: float | None      = Field(default=None)
    test_rmse: float | None    = Field(default=None)
    q2: float | None           = Field(default=None)
    test_mcc: float | None     = Field(default=None)
    test_roc_auc: float | None = Field(default=None)
    cv_mcc: float | None       = Field(default=None)

    model_path: str   = Field(default="")   # serialized model file or directory (relative)
    metrics: dict     = Field(default_factory=dict, sa_type=JSON)

    created_at: datetime = Field(default_factory=datetime.now, sa_type=DateTime)


# ---------------------------------------------------------------------------
# QSARModelMember
# ---------------------------------------------------------------------------

class QSARModelMember(SQLModel, table=True):
    """
    One molecule a model was trained or tested on: which side of the split it fell on, its CV fold,
    the label the model saw and what the model said about it. The model diagnostics (observed vs
    predicted, ROC, confusion matrix, ...) are read from here, never re-predicted.

    observed is the label after any class threshold. predicted/probability come from the final
    model; oof_* from the CV model that did not see the molecule (NULL without CV or for test rows).
    probability = P(the higher-valued class) for binary classifiers, else NULL.
    """

    __tablename__ = TABLE_QSAR_MODEL_MEMBERS

    model_id: int    = Field(foreign_key=f"{TABLE_QSAR_MODELS}.id", primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", primary_key=True, index=True)
    role: str        = Field(default="train")  # "train" | "test"
    fold: int | None = Field(default=None)

    observed: float
    predicted: float
    probability: float | None     = Field(default=None)
    oof_predicted: float | None   = Field(default=None)
    oof_probability: float | None = Field(default=None)


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
        # a model's top N, by value or by positive-class probability, is an index range scan
        Index("ix_qsar_predictions_model_value", "model_id", "value"),
        Index("ix_qsar_predictions_model_probability", "model_id", "positive_probability"),
    )

    id: int | None   = Field(default=None, primary_key=True)
    molecule_id: int = Field(foreign_key=f"{TABLE_MOLECULES}.id", index=True)
    model_id: int    = Field(foreign_key=f"{TABLE_QSAR_MODELS}.id")

    value: float
    confidence: float | None = Field(default=None)
    positive_probability: float | None = Field(default=None)  # binary P(higher-valued class)
    # mean Tanimoto to the model's nearest training molecules (applicability domain); None when
    # the model stored no training fingerprints
    ad_similarity: float | None = Field(default=None)


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
                "positive_probability": p.get("positive_probability"),
            }
            for p in predictions
        ]
