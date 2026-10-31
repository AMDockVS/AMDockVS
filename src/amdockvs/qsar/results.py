"""Reading a model's predictions and using them as a filter: list, join with names, cut, promote."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, update as sql_update
from sqlalchemy.orm import aliased
from sqlmodel import select

from amdockvs.models import MoleculeRecord, QSARModelRecord, QSARPredictionRecord
from amdockvs.molecules.storage import ShardStore
from amdockvs.project.sets import QSARModelRef


class ResultsMixin:
    def shard_predictions(self, *, model: QSARModelRef | int):
        """A model's predictions over the library's shards, as a frame (key, value, confidence,
        positive_probability, ad_similarity).
        Only the active generation's shards are read: an older one is no longer the library.

        ponytail: the whole frame in RAM (~24 B/molecule, 2.4 GB at 10^8); a per-file top-N
        before the concat when a library gets there."""
        import pandas as pd

        self.runtime._require_active_project()
        directory = self._shard_predictions_dir(self._model_id(model))
        paths = [directory / f"{int(row['id'])}.parquet"
                 for row in ShardStore(self.runtime.workspace.project_db).iter_rows(batch_size=1000)]
        frames = [pd.read_parquet(path) for path in paths if path.is_file()]
        if not frames:
            return pd.DataFrame({"key": pd.Series(dtype="int64"), "value": pd.Series(dtype=float),
                                 "confidence": pd.Series(dtype=float),
                                 "positive_probability": pd.Series(dtype=float),
                                 "ad_similarity": pd.Series(dtype=float)})
        frame = pd.concat(frames, ignore_index=True)
        if "ad_similarity" not in frame:  # shards scored before the domain was stored
            frame["ad_similarity"] = float("nan")
        return frame

    def _is_classifier(self, model_id: int) -> bool:
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, model_id)
        if record is None:
            raise ValueError(f"Unknown QSAR model id: {model_id}")
        return record.task == "classification"

    def promote_shard_predictions(
        self,
        *,
        model: QSARModelRef | int,
        top_n: int | None = None,
        cutoff: float | None = None,
        higher_is_better: bool = True,
    ) -> dict[str, Any]:
        """The cut of ``cut_predictions`` over the library's predictions: the records kept become
        molecule rows carrying their prediction, so everything that reads rows (the Predictions
        view, ``cut_predictions``, docking) has them. Ties break by key. Returns {"scored": n,
        "kept": n, "molecule_ids": the kept, best first}."""
        if top_n is None and cutoff is None:
            raise ValueError("promote_shard_predictions needs top_n and/or cutoff.")
        model_id = self._model_id(model)
        frame = self.shard_predictions(model=model_id)
        scored = len(frame)
        classification = self._is_classifier(model_id)
        score = "positive_probability" if classification else "value"
        if classification and frame[score].isna().any():
            raise ValueError(
                "Classifier ranking requires positive-class probabilities: only binary classifiers "
                "with probabilities can rank."
            )
        if cutoff is not None:
            frame = frame[frame[score] >= float(cutoff) if higher_is_better else frame[score] <= float(cutoff)]
        frame = frame.sort_values([score, "key"], ascending=[not higher_is_better, True])
        if top_n is not None:
            frame = frame.head(max(0, int(top_n)))
        kept = list(frame[["key", "value", "confidence", "positive_probability", "ad_similarity"]]
                    .itertuples(index=False, name=None))
        promoted = ShardStore(self.runtime.workspace.project_db).promote(int(key) for key, *_ in kept)
        ids = [promoted[key] for key, *_ in kept if key in promoted]
        with self.runtime.workspace.project_db.get_session() as session:
            for start in range(0, len(ids), 900):
                for row in session.exec(
                    select(QSARPredictionRecord)
                    .where(QSARPredictionRecord.model_id == model_id)
                    .where(QSARPredictionRecord.molecule_id.in_(ids[start:start + 900]))
                ).all():
                    session.delete(row)
            session.flush()
            session.add_all(
                QSARPredictionRecord(
                    molecule_id=promoted[key], model_id=model_id, value=value,
                    confidence=None if confidence != confidence else float(confidence),  # NaN = none
                    positive_probability=None if probability != probability else float(probability),
                    ad_similarity=None if domain != domain else float(domain),
                )
                for key, value, confidence, probability, domain in kept if key in promoted
            )
            session.commit()
        return {"scored": scored, "kept": len(ids), "molecule_ids": ids}

    def list_predictions(
        self,
        *,
        model: QSARModelRef | int | None = None,
        ligand_ids: Sequence[int] | None = None,
    ) -> list[QSARPredictionRecord]:
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            statement = select(QSARPredictionRecord)
            if model is not None:
                statement = statement.where(
                    QSARPredictionRecord.model_id == self._model_id(model)
                )
            if ligand_ids is not None:
                statement = statement.where(QSARPredictionRecord.molecule_id.in_([int(i) for i in ligand_ids]))
            return list(session.exec(statement.order_by(QSARPredictionRecord.id)))

    def prediction_rows(self, *, model: QSARModelRef | int) -> list[dict[str, Any]]:
        """Predictions joined with the ligand name, for the Predictions table:
        [{molecule_id, name, value, confidence, positive_probability, ad_similarity}] ordered by
        ligand id. Excluded ligands are left
        out, as in every other ligand listing."""
        self.runtime._require_active_project()
        model_id = self._model_id(model)
        with self.runtime.workspace.project_db.get_session() as session:
            rows = list(session.exec(
                select(QSARPredictionRecord)
                .where(QSARPredictionRecord.model_id == model_id)
                .order_by(QSARPredictionRecord.molecule_id)
            ))
            names = {
                int(m.id): str(m.name or "")
                for m in session.exec(
                    select(MoleculeRecord)
                    .where(MoleculeRecord.id.in_([int(r.molecule_id) for r in rows] or [0]))
                    .where(MoleculeRecord.excluded == False)  # noqa: E712
                )
            }
        return [
            {"molecule_id": int(r.molecule_id), "name": names[int(r.molecule_id)],
             "value": float(r.value), "confidence": None if r.confidence is None else float(r.confidence),
             "positive_probability": None if r.positive_probability is None else float(r.positive_probability),
             "ad_similarity": r.ad_similarity}
            for r in rows if int(r.molecule_id) in names
        ]

    def cut_predictions(
        self,
        *,
        model: QSARModelRef | int,
        top_n: int | None = None,
        cutoff: float | None = None,
        higher_is_better: bool = True,
        exclude_rest: bool = False,
        ids_limit: int = 0,
    ) -> dict[str, Any]:
        """Use a model's predictions as a filter: keep the ligands at or beyond ``cutoff`` (>= when
        higher is better, <= otherwise) and, of those, at most the ``top_n`` best (ties break by
        id). With ``exclude_rest`` every other ligand the model scored is marked excluded
        (reversible); ligands it could not score, and ligands already excluded, are left alone.

        Binary classifiers rank/filter by P(the higher-valued class), not the predicted class
        label or max-class confidence.

        The cut is a subquery over the predictions, so nothing here grows with the library: the
        exclusion is one UPDATE and only ``ids_limit`` ids ever come back. Returns {"kept": n,
        "rest": n, "excluded": how many were marked, "ids": the first ``ids_limit`` kept, best first}."""
        if top_n is None and cutoff is None:
            raise ValueError("cut_predictions needs top_n and/or cutoff.")
        self.runtime._require_active_project()
        model_id = self._model_id(model)
        classification = self._is_classifier(model_id)
        scored_ligand = aliased(MoleculeRecord)  # aliased: the UPDATE below targets the table itself
        scored = (
            select(QSARPredictionRecord.molecule_id)
            .join(scored_ligand, scored_ligand.id == QSARPredictionRecord.molecule_id)
            .where(QSARPredictionRecord.model_id == model_id, scored_ligand.excluded == False)  # noqa: E712
        )
        value = QSARPredictionRecord.positive_probability if classification else QSARPredictionRecord.value
        kept = scored.order_by(value.desc() if higher_is_better else value.asc(), QSARPredictionRecord.molecule_id)
        if cutoff is not None:
            kept = kept.where(value >= float(cutoff) if higher_is_better else value <= float(cutoff))
        if top_n is not None:
            kept = kept.limit(max(0, int(top_n)))
        how = (f"top {int(top_n)}" if top_n is not None else "") + (
            f"{' ' if top_n is not None else ''}{'>=' if higher_is_better else '<='} {float(cutoff):g}"
            if cutoff is not None else "")
        with self.runtime.workspace.project_db.get_session() as session:
            if classification and session.exec(scored.where(value.is_(None)).limit(1)).first() is not None:
                raise ValueError(
                    "Classifier ranking requires positive-class probabilities: multiclass or "
                    "non-probabilistic classifiers cannot use this ranking."
                )
            n_scored = int(session.exec(select(func.count()).select_from(scored.subquery())).one())
            n_kept = int(session.exec(select(func.count()).select_from(kept.subquery())).one())
            ids = [int(i) for i in session.exec(kept.limit(min(int(ids_limit), n_kept)))] if ids_limit else []
            excluded = 0
            if exclude_rest and n_kept < n_scored:
                result = session.exec(
                    sql_update(MoleculeRecord)
                    .where(MoleculeRecord.id.in_(scored), MoleculeRecord.id.not_in(kept))
                    .values(excluded=True, exclusion_reason=f"qsar: outside the cut of model #{model_id} ({how})")
                )
                excluded = int(getattr(result, "rowcount", 0) or 0)
                session.commit()
        return {"kept": n_kept, "rest": n_scored - n_kept, "excluded": excluded, "ids": ids}
