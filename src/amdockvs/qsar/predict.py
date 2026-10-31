"""Scoring molecules with a model: rows in the database, or the library's shards as a job."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from sqlmodel import select

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR, SHARDS_PRIORITY
from amdockvs.models import QSARModelRecord, QSARPredictionRecord
from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef
from amdockvs.qsar.jobs import ShardPredictJobParams, predict_shards_job


class PredictMixin:
    def predict(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        self.runtime._require_active_project()
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            features = self._features_for(fitted)
            ids, x_rows, _names, _missing = self._design_matrix(
                session, ligands, feature_kind=fitted.feature_kind, features=features,
                fp_radius=int(fitted.fp_radius or 2), fp_nbits=int(fitted.fp_nbits or 2048),
            )
            if not ids:
                return {"model_id": int(record.id or 0), "predicted": 0, "skipped_missing_descriptor": len(ligands)}
            x_matrix = np.asarray(x_rows, dtype=float)
            values = fitted.predict(x_matrix)
            confidence = fitted.predict_confidence(x_matrix)
            positive_probability = fitted.predict_positive_probability(x_matrix)
            model_id = int(record.id or 0)
            if replace:
                for row in session.exec(
                    select(QSARPredictionRecord)
                    .where(QSARPredictionRecord.model_id == model_id)
                    .where(QSARPredictionRecord.molecule_id.in_(ids))
                ).all():
                    session.delete(row)
                session.flush()
            for i, ligand_id in enumerate(ids):
                session.add(
                    QSARPredictionRecord(
                        molecule_id=ligand_id,
                        model_id=model_id,
                        value=float(values[i]),
                        confidence=None if confidence is None else float(confidence[i]),
                        positive_probability=None if positive_probability is None else float(positive_probability[i]),
                    )
                )
            session.commit()
            return {
                "model_id": model_id,
                "predicted": len(ids),
                "skipped_missing_descriptor": len(ligands) - len(ids),
            }

    def _shard_predictions_dir(self, model_id: int) -> Path:
        return self._artifact_path(f"shard_predictions_{int(model_id)}")

    def predict_shards(
        self,
        *,
        model: QSARModelRef | int,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
    ) -> str:
        """``predict`` over the library's shards, as a job at ``SHARDS_PRIORITY``. Every shard
        is scored into a file of its own, keyed by ``io.shards.shard_key``; nothing enters the
        database until ``promote_shard_predictions`` cuts it. Scoring again overwrites each
        shard's file. Returns the job id."""
        self.runtime._require_active_project()
        model_id = self._model_id(model)
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, model_id)
            if record is None:
                raise ValueError(f"Unknown QSAR model id: {model_id}")
        params = ShardPredictJobParams(
            model_path=str(self._project_root() / record.model_path),
            output_dir=str(self._shard_predictions_dir(model_id)),
        )
        return self.runtime.submit_job(
            predict_shards_job, params=params.model_dump(mode="python"), executor_name=executor_name,
            depends_on=depends_on, priority=SHARDS_PRIORITY,
        )
