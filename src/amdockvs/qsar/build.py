"""Building models: previewing a split, training and registering one, loading an external one.

A trained model leaves three things: its registry row (``qsar_models``, headline scores as
columns), one ``qsar_model_members`` row per molecule it was trained or tested on, and the fitted
model on disk. The diagnostics read the members; nothing is re-predicted to draw them.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR
from amdockvs.core.normalize import PathLike
from amdockvs.models import QSARModelRecord
from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.project.sets import MoleculeSetRef
from amdockvs.qsar.features import fingerprint_matrix, rdkit2d_blocks
from amdockvs.qsar.jobs import TrainJobParams, train_models_job
from amdockvs.qsar.modeling import load_model, require_sklearn, save_model, supported_algorithms
from amdockvs.qsar.splitters import NEEDS_STRUCTURES, SPLITTERS, assign_split, molecule_groups
from amdockvs.qsar.training import feature_type_of

FEATURE_SOURCES = ("rdkit2d", "ecfp4")


def feature_kind_of(feature_source: str) -> str:
    source = str(feature_source).lower()
    if source in {"ecfp4", "fingerprint", "morgan"}:
        return "ecfp4"
    if source in {"rdkit2d", "rdkit_2d", "rdkit-2d"}:
        return "rdkit2d"
    raise ValueError(f"Unknown feature source '{feature_source}'. Choose one of {', '.join(FEATURE_SOURCES)}.")


def _labels(y_raw: np.ndarray, task: str, class_threshold: float | None) -> np.ndarray:
    if task != "classification":
        return y_raw
    return (y_raw >= float(class_threshold)).astype(int) if class_threshold is not None else y_raw.astype(int)


class BuildMixin:
    @staticmethod
    def supported_algorithms(task: str = "regression") -> tuple[str, ...]:
        return supported_algorithms(task)

    def _labeled(self, session, molecule_set, endpoint: str):
        """(activity by ligand id, the labeled ligand records) for an endpoint."""
        ligands = self._ligands(molecule_set)
        activity_by_ligand = self._latest_activity_by_ligand(session, endpoint)
        return activity_by_ligand, [r for r in ligands if int(r.id or 0) in activity_by_ligand]

    def _split_groups(self, session, split: str, records, used_ids, seed: int):
        """Group labels for the grouped splitters, aligned to ``used_ids``; None otherwise."""
        if split not in NEEDS_STRUCTURES:
            return None
        by_id = {int(r.id or 0): r for r in records}
        return molecule_groups(split, [self._ligand_mol(session, by_id[mid]) for mid in used_ids], seed=seed)

    def split_preview(
        self,
        *,
        endpoint: str,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        split: str = "scaffold",
        test_size: float = 0.3,
        class_threshold: float | None = None,
        seed: int = 0,
        bins: int = 10,
    ) -> dict[str, Any]:
        """Train/test label distribution for an endpoint WITHOUT training, so you can see whether the
        split balances the labels — not just chemical space. The grouped splitters (scaffold,
        Butina, MaxMin) keep whole groups together but do NOT stratify by class, so an imbalanced
        endpoint can land most actives on one side. Returns {task, categories, train[], test[],
        n_train, n_test, note} — counts per class (classification) or per value-bin (regression)."""
        self.runtime._require_active_project()
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("split_preview requires endpoint.")
        with self.runtime.workspace.project_db.get_session() as session:
            activity_by_ligand, labeled = self._labeled(session, molecule_set, endpoint_text)
            used_ids = [int(r.id or 0) for r in labeled]
            if len(used_ids) < 2:
                raise ValueError(f"Only {len(used_ids)} labeled ligand(s) for '{endpoint_text}'.")
            groups = self._split_groups(session, split, labeled, used_ids, seed)
        y = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
        kind = self.endpoint_kinds().get(endpoint_text, "")
        is_class = kind == "categorical" or class_threshold is not None or set(np.unique(y).tolist()) <= {0.0, 1.0}
        task = "classification" if is_class else "regression"
        is_test, _folds = assign_split(split, y=_labels(y, task, class_threshold), task=task, groups=groups,
                                       test_size=float(test_size), seed=int(seed))
        train_idx, test_idx = np.flatnonzero(~is_test), np.flatnonzero(is_test)
        if is_class:
            yb = _labels(y, task, class_threshold)
            classes = sorted(set(yb.tolist()))
            train = [int((yb[train_idx] == c).sum()) for c in classes]
            test = [int((yb[test_idx] == c).sum()) for c in classes]
            # active-class fraction on each side — the imbalance the split may distort
            pos = classes[-1] if classes else 1
            fr = lambda idx: float((yb[idx] == pos).mean()) if len(idx) else 0.0
            note = f"class {pos} fraction - train {fr(train_idx):.3f} vs test {fr(test_idx):.3f}"
            return {"task": "classification", "categories": [str(c) for c in classes],
                    "train": train, "test": test, "n_train": len(train_idx), "n_test": len(test_idx), "note": note}
        lo, hi = float(y.min()), float(y.max())
        edges = np.linspace(lo, hi, int(bins) + 1) if hi > lo else np.array([lo, lo + 1.0])
        cats = [f"{edges[i]:.2g}" for i in range(len(edges) - 1)]
        train = [int(v) for v in np.histogram(y[train_idx], bins=edges)[0]]
        test = [int(v) for v in np.histogram(y[test_idx], bins=edges)[0]]
        mean = lambda idx: float(y[idx].mean()) if len(idx) else float("nan")
        note = f"mean value - train {mean(train_idx):.3f} vs test {mean(test_idx):.3f}"
        return {"task": "regression", "categories": cats, "train": train, "test": test,
                "n_train": len(train_idx), "n_test": len(test_idx), "note": note}

    def submit_train(
        self,
        *,
        endpoint: str,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        algorithm: str | Sequence[str] = "linear_regression",
        task: str = "regression",
        feature_source: str = "rdkit2d",
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        name: str = "",
        test_size: float = 0.0,
        split: str = "random",
        cv_folds: int = 0,
        class_threshold: float | None = None,
        corr_threshold: float = 0.95,
        y_scramble: int = 0,
        hyperparams: dict[str, Any] | None = None,
        seed: int = 0,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
    ) -> dict[str, Any]:
        """Submit the training of one model per ``algorithm`` as a job, one task per model.

        The molecules are labeled and split here, so a split that cannot work fails before
        anything runs; the features a molecule lacks are computed first by a descriptor job the
        train job waits for. A model identical to one already stored (same endpoint values, same
        molecules, same settings but its name) is not trained again: it is listed in ``existing``.
        Returns {"job_id", "descriptor_job_id", "identities": {algorithm: identity},
        "existing": {algorithm: model id}}; ``job_id`` is None when every model already exists.
        See ``train`` for the parameters."""
        self.runtime._require_active_project()
        require_sklearn()
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("train requires endpoint.")
        if split not in SPLITTERS:
            raise ValueError(f"split must be one of {', '.join(SPLITTERS)}.")
        if not 0 <= float(test_size) < 1:
            raise ValueError("test_size must be >=0 and <1; use 0 to disable the holdout.")
        algorithms = [algorithm] if isinstance(algorithm, str) else list(algorithm)
        kind = feature_kind_of(feature_source)
        with self.runtime.workspace.project_db.get_session() as session:
            activity_by_ligand, labeled = self._labeled(session, molecule_set, endpoint_text)
            ids = [int(r.id or 0) for r in labeled]
            if len(ids) < 2:
                raise ValueError(f"train needs >=2 labeled ligands for '{endpoint_text}'; got {len(ids)}.")
            groups = self._split_groups(session, split, labeled, ids, seed)
        y = _labels(np.asarray([activity_by_ligand[mid] for mid in ids], dtype=float), task, class_threshold)
        if task == "classification" and np.unique(y).size < 2:
            raise ValueError(
                "All ligands fall in a single class after labeling - a model can't be trained. "
                "For a categorical endpoint don't set a class threshold (the value is already the "
                "label); for a continuous one, pick a threshold that leaves both classes populated."
            )
        is_test, folds = assign_split(split, y=y, task=task, groups=groups, test_size=float(test_size),
                                      cv_folds=int(cv_folds), seed=int(seed))
        n_test = int(is_test.sum())
        if float(test_size) > 0 and (len(ids) - n_test < 2 or not n_test):
            raise ValueError(
                "The requested holdout cannot leave at least two training molecules and "
                "one test molecule. Add more molecules or reduce the test fraction."
            )
        if task == "classification" and np.unique(y[~is_test]).size < 2:
            raise ValueError(
                "The requested split leaves only one class in training. Add more labeled "
                "molecules or choose a different split or seed."
            )

        settings = {
            "endpoint": endpoint_text, "task": task, "kind": kind, "split": split,
            "test_size": float(test_size), "cv_folds": int(cv_folds), "y_scramble": int(y_scramble),
            "class_threshold": class_threshold, "corr_threshold": float(corr_threshold),
            "hyperparams": hyperparams or None, "seed": int(seed),
            "fp_radius": int(fp_radius) if kind == "ecfp4" else None,
            "fp_nbits": int(fp_nbits) if kind == "ecfp4" else None,
        }
        data = sorted((mid, float(activity_by_ligand[mid])) for mid in ids)
        stored = {(m.metrics or {}).get("identity"): int(m.id or 0) for m in self.list_models()}
        identities, existing, specs = {}, {}, []
        for algo in algorithms:
            spec = {**settings, "algorithm": algo}
            identity = hashlib.sha256(json.dumps([spec, data], sort_keys=True).encode()).hexdigest()
            identities[algo] = identity
            if identity in stored:
                existing[algo] = stored[identity]
            else:
                specs.append({**spec, "identity": identity, "name": name if len(algorithms) == 1 else ""})
        request = {"job_id": None, "descriptor_job_id": None, "identities": identities, "existing": existing}
        if not specs:
            return request

        with self.runtime.workspace.project_db.get_session() as session:
            if kind == "rdkit2d":
                have = set(rdkit2d_blocks(session, ids))
            else:
                have = set(fingerprint_matrix(session, ids, radius=fp_radius, nbits=fp_nbits))
        missing = [mid for mid in ids if mid not in have]
        if missing:
            request["descriptor_job_id"] = self.runtime.qsar.compute_descriptors(
                molecule_ids=missing, rdkit2d=kind == "rdkit2d", compute_fingerprints=kind == "ecfp4",
                fp_radius=fp_radius, fp_nbits=fp_nbits, executor_name=executor_name,
            )
        # ponytail: the split file stays in tmp/qsar; a few KB per training.
        data_path = self._project_root() / "tmp" / "qsar" / f"split_{specs[0]['identity'][:16]}.npz"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(data_path, ids=np.asarray(ids, dtype=np.int64), y=y, is_test=is_test, folds=folds,
                 groups=np.asarray([] if groups is None else [str(g) for g in groups], dtype=str))
        if kind == "rdkit2d":
            from amdockvs.chemistry.descriptors import rdkit2d_descriptor_names

            feature_names = list(rdkit2d_descriptor_names())
        else:
            feature_names = []
        params = TrainJobParams(
            data_path=str(data_path), output_dir=str(self._artifact_path("x").parent), feature_kind=kind,
            feature_names=feature_names, fp_radius=int(fp_radius), fp_nbits=int(fp_nbits), specs=specs,
        )
        request["job_id"] = self.runtime.submit_job(
            train_models_job, params=params.model_dump(mode="python"), executor_name=executor_name,
            depends_on=[request["descriptor_job_id"]] if missing else None,
        )
        return request

    def train(self, *, algorithm: str = "linear_regression", timeout_s: float = 3600.0,
              **params: Any) -> QSARModelRecord:
        """Fit a model from the ligands of ``molecule_set`` that have an activity for ``endpoint``,
        waiting for its job (``submit_train`` does not wait, and takes several algorithms).

        feature_source: 'rdkit2d' (the full RDKit 2D block, computed and cached by a descriptor
        job on first use) or 'ecfp4' (the stored Morgan fingerprint). For rdkit2d the preprocessing
        (impute → drop zero-variance → drop |r|>corr_threshold → scale) lives inside the pipeline,
        so CV scores are leakage-free. ``split`` is one of ``splitters.SPLITTERS``; the grouped ones
        (scaffold, Butina, MaxMin) keep a group out of both sides, in the holdout and in the CV
        folds alike. cv_folds>=2 adds a training-only CV score (Q²/MCC) and an out-of-fold
        prediction per training molecule; label-permutation checks (y_scramble>=1 refits) also use
        only training molecules. class_threshold binarizes a *continuous* endpoint (active = value
        >= threshold); leave None for categorical endpoints. Training the same model on the same
        data again returns the stored one."""
        request = self.submit_train(algorithm=algorithm, **params)
        jobs = [j for j in (request["descriptor_job_id"], request["job_id"]) if j]
        for job_id, status in self.runtime.wait_for_jobs(jobs, timeout_s=timeout_s).items():
            if status.status != "completed":
                raise ValueError(f"Training failed ({status.status}): {status.error}")
        identity = request["identities"][algorithm]
        for record in self.list_models():
            if (record.metrics or {}).get("identity") == identity:
                return record
        raise ValueError("The training job finished without storing the model.")

    def _store_artifact(self, session, record: QSARModelRecord, fitted) -> QSARModelRecord:
        artifact = self._artifact_path(f"model_{int(record.id or 0)}.joblib")
        save_model(artifact, fitted)
        record.model_path = str(artifact.relative_to(self._project_root()))
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    def load_external_model(
        self,
        *,
        path: PathLike,
        name: str,
        endpoint: str = "",
    ) -> QSARModelRecord:
        """Register a pre-built model artifact (a joblib FittedModel) for prediction. No
        training input needed — the artifact already carries features + task. It records no
        members, so it has no diagnostics until ``evaluate`` scores it on labeled molecules."""
        self.runtime._require_active_project()
        source_path = Path(path).expanduser().resolve()
        fitted = load_model(source_path)  # validates format
        with self.runtime.workspace.project_db.get_session() as session:
            record = QSARModelRecord(
                name=str(name or source_path.stem),
                algorithm=fitted.algorithm,
                target=str(endpoint or ""),
                feature_type=feature_type_of(fitted.feature_kind),
                source="external",
                task=fitted.task,
                engine="sklearn",
                metrics={"features": f"{fitted.feature_kind}:{len(fitted.feature_names)}",
                         "feature_kind": fitted.feature_kind,
                         "classes": [int(c) for c in fitted.classes],
                         "positive_class": int(max(fitted.classes)) if len(fitted.classes) == 2 else None},
            )
            session.add(record)
            session.flush()
            return self._store_artifact(session, record, fitted)

    def correlation_matrix(
        self,
        *,
        feature_source: str = "rdkit2d",
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        corr_threshold: float = 0.95,
    ) -> dict[str, Any]:
        """Pearson correlation matrix across the RDKit 2D block (over the project's ligands) — the
        redundancy the training pipeline prunes. ecfp4 is unsupported (2048 binary bits). Returns
        {labels, matrix, dropped, n_ligands} where dropped = features the |r|>corr_threshold filter
        would remove (kept feature listed first)."""
        self.runtime._require_active_project()
        if feature_kind_of(feature_source) != "rdkit2d":
            raise ValueError("Correlation heatmap is for descriptor blocks, not fingerprints.")
        require_sklearn()
        from amdockvs.chemistry.descriptors import rdkit2d_descriptor_names
        from amdockvs.qsar.correlation import CorrelationFilter

        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            _ids, x_rows, names, _missing = self._design_matrix(
                session, ligands, feature_kind="rdkit2d", features=tuple(rdkit2d_descriptor_names()),
                fp_radius=2, fp_nbits=2048,
            )
        if len(x_rows) < 2:
            raise ValueError("Need >=2 ligands with the RDKit 2D block; compute it first with "
                             "compute_descriptors(rdkit2d=True) or by training an rdkit2d model.")
        x_matrix = np.asarray(x_rows, dtype=float)
        # median-impute gaps, then drop zero-variance columns (correlation is undefined there)
        medians = np.nanmedian(x_matrix, axis=0)
        nan_idx = np.where(np.isnan(x_matrix))
        x_matrix[nan_idx] = np.take(medians, nan_idx[1])
        keep = x_matrix.var(axis=0) > 0
        x_matrix = x_matrix[:, keep]
        labels = [n for n, k in zip(names, keep) if k]
        corr = np.nan_to_num(np.corrcoef(x_matrix, rowvar=False), nan=0.0)
        support = CorrelationFilter(threshold=float(corr_threshold)).fit(x_matrix).get_support()
        dropped = [n for n, kept in zip(labels, support) if not kept]
        return {"labels": labels, "matrix": corr.tolist(), "dropped": dropped, "n_ligands": int(x_matrix.shape[0])}
