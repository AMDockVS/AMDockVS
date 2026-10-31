"""Scoring molecules with a model: rows in the database, or the library's shards as a job."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlmodel import func, select

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR, SHARDS_PRIORITY
from amdockvs.models import QSARModelRecord, QSARPredictionRecord
from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef
from amdockvs.qsar.jobs import PredictRowsJobParams, ShardPredictJobParams, predict_rows_job, predict_shards_job


class PredictMixin:
    def submit_predict(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        batch_size: int = 1000,
    ) -> str:
        """Score the ligands of ``molecule_set`` with a model as a job, a batch per task; the
        features come from each structure, as for the shards. Scoring again replaces a
        molecule's prediction. Returns the job id."""
        self.runtime._require_active_project()
        model_id = self._model_id(model)
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, model_id)
            if record is None:
                raise ValueError(f"Unknown QSAR model id: {model_id}")
        scope = self._qsar_scope(molecule_set)
        params = PredictRowsJobParams(
            batch_size=batch_size, only_missing=False, model_id=model_id,
            model_path=str(self._project_root() / record.model_path),
            molecule_filters={**dict(scope.filters or {}), "excluded": False},
            molecule_set_id=scope.source_set_id,
        )
        return self.runtime.submit_job(
            predict_rows_job, params=params.model_dump(mode="python"), executor_name=executor_name,
            depends_on=depends_on,
        )

    def predict(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        timeout_s: float = 3600.0,
    ) -> dict[str, Any]:
        """``submit_predict`` and wait: {"model_id", "predicted", "skipped_missing_descriptor"}."""
        model_id = self._model_id(model)
        job_id = self.submit_predict(model=model_id, molecule_set=molecule_set, executor_name=executor_name)
        status = self.runtime.wait_for_jobs([job_id], timeout_s=timeout_s)[job_id]
        if status.status != "completed":
            raise ValueError(f"Prediction failed ({status.status}): {status.error}")
        ids = [int(r.id or 0) for r in self._ligands(molecule_set)]
        with self.runtime.workspace.project_db.get_session() as session:
            scored = session.exec(
                select(func.count()).select_from(QSARPredictionRecord)
                .where(QSARPredictionRecord.model_id == model_id)
                .where(QSARPredictionRecord.molecule_id.in_(ids or [0]))
            ).one()
        return {"model_id": model_id, "predicted": int(scored), "skipped_missing_descriptor": len(ids) - int(scored)}

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
