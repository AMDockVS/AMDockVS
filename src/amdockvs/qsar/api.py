"""Lean QSAR API — activities, descriptor-based features, simple sklearn model templates.

No dataset/split/role machinery: train takes a molecule set + an activity endpoint and
fits a template estimator; predict scores any molecule set. Features come straight from
the descriptor columns on MoleculeRecord (populated by the descriptor job). Designed to
work standalone in script/notebook mode; the UI just calls these methods.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from sqlalchemy import func, update as sql_update
from sqlalchemy.orm import aliased
from sqlmodel import select

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR, RESOURCE_QSAR_MODELS, SHARDS_PRIORITY
from amdockvs.core.vocab import ReprType
from amdockvs.models import (
    ActivityRecord,
    FingerprintRecord,
    MoleculeRecord,
    MoleculeRepresentation,
    QSARModelRecord,
    QSARPredictionRecord,
)
from amdockvs.models.descriptors import FingerprintType
from amdockvs.qsar.activities import ActivitiesMixin
from amdockvs.qsar.jobs import (
    DescriptorJobParams,
    ShardPredictJobParams,
    calculate_molecule_alerts_job,
    calculate_molecule_descriptors_job,
    predict_shards_job,
)
from amdockvs.qsar.modeling import (
    DEFAULT_QSAR_FEATURES,
    FittedModel,
    classification_metrics,
    feature_importance,
    fit_model,
    load_model,
    normalize_feature_names,
    regression_metrics,
    save_model,
    supported_algorithms,
)
from amdockvs.core.normalize import PathLike
from amdockvs.molecules.scopes import MoleculeScope, as_molecule_scope, is_molecule_scope, scope_payload
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.molecules.storage import ShardStore
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef
from amdockvs.workflows.rules import apply_workflow_filters

QSAR_WORKFLOW = "qsar"


@dataclass
class QSARAPI(ActivitiesMixin):
    runtime: Any

    # --- scope / feature helpers ---------------------------------------------
    def _qsar_scope(self, molecule_set: MoleculeSetRef | MoleculeScope | int | None) -> MoleculeScope:
        if is_molecule_scope(molecule_set):
            scope = as_molecule_scope(molecule_set)
            assert scope is not None
            return MoleculeScope(
                filters=apply_workflow_filters(scope.filters, workflow=QSAR_WORKFLOW, role="ligand"),
                source_set_id=scope.source_set_id,
                order=tuple(scope.order or ("id",)),
                limit=scope.limit,
            )
        if molecule_set is None:
            scope = self.runtime.molecules.select(role="ligand", workflow=QSAR_WORKFLOW).scope
        else:
            scope = self.runtime.molecules.select(source=molecule_set, role="ligand", workflow=QSAR_WORKFLOW).scope
        assert isinstance(scope, MoleculeScope)
        return scope

    def _ligands(self, molecule_set) -> list[MoleculeRecord]:
        return list(self.runtime.molecules.stream(self._qsar_scope(molecule_set)))

    @staticmethod
    def _feature_vector(record: MoleculeRecord, feature_names: Sequence[str]) -> list[float] | None:
        values: list[float] = []
        for name in feature_names:
            value = getattr(record, name, None)
            if value is None:
                return None
            values.append(float(value))
        return values

    def _design_matrix(
        self, session, ligands, *, feature_kind: str, features, fp_radius: int, fp_nbits: int
    ) -> tuple[list[int], list[list[float]], tuple[str, ...], int]:
        """(used_ids, x_rows, feature_names, n_missing). feature_kind 'descriptors' reads the named
        columns; 'rdkit2d' reads/computes the full RDKit block (per-descriptor gaps become NaN for
        the pipeline's imputer); 'ecfp4' unpacks each ligand's stored Morgan fingerprint."""
        if feature_kind == "rdkit2d":
            block = self._rdkit2d_matrix(session, ligands)
            names = tuple(features)
            ids, rows, missing = [], [], 0
            for rec in ligands:
                values = block.get(int(rec.id or 0))
                if not values:  # whole block failed (unparseable structure) → drop the row
                    missing += 1
                    continue
                ids.append(int(rec.id or 0))
                rows.append([float(values.get(n, np.nan)) for n in names])
            return ids, rows, names, missing
        if feature_kind == "ecfp4":
            fp_map = self._fingerprint_matrix(
                session, [int(r.id or 0) for r in ligands], radius=fp_radius, nbits=fp_nbits
            )
            names = tuple(f"bit_{i}" for i in range(int(fp_nbits)))
            ids, rows, missing = [], [], 0
            for rec in ligands:
                vector = fp_map.get(int(rec.id or 0))
                if vector is None:
                    missing += 1
                    continue
                ids.append(int(rec.id or 0))
                rows.append([float(v) for v in vector])
            return ids, rows, names, missing
        ids, rows, missing = [], [], 0
        for rec in ligands:
            vector = self._feature_vector(rec, features)
            if vector is None:
                missing += 1
                continue
            ids.append(int(rec.id or 0))
            rows.append(vector)
        return ids, rows, tuple(features), missing

    @staticmethod
    def _score(task: str, y_true: np.ndarray, fitted: FittedModel, x_matrix: np.ndarray) -> dict[str, Any]:
        """Task-appropriate metrics; for classification, pass positive-class proba for ROC-AUC."""
        if task == "classification":
            y_score = None
            estimator = fitted.estimator
            if hasattr(estimator, "predict_proba") and len(getattr(fitted, "classes", ()) or ()) == 2:
                y_score = np.asarray(estimator.predict_proba(np.asarray(x_matrix, dtype=float)))[:, 1]
            return classification_metrics(y_true, fitted.predict(x_matrix), y_score)
        return regression_metrics(y_true, fitted.predict(x_matrix))

    def _scaffold_keys(self, records) -> dict[int, str]:
        """molecule_id -> Bemis-Murcko scaffold SMILES (empty string when unparseable)."""
        from amdockvs.chemistry.structures import molecule_mol

        try:
            from rdkit import Chem
            from rdkit.Chem.Scaffolds import MurckoScaffold
        except ImportError:
            return {}
        out: dict[int, str] = {}
        for rec in records:
            mid = int(rec.id or 0)
            mol = molecule_mol(self.runtime.workspace.project_db, rec, remove_hs=True)
            out[mid] = "" if mol is None else MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
        return out

    def _latest_activity_by_ligand(self, session, endpoint: str) -> dict[int, float]:
        rows = session.exec(
            select(ActivityRecord)
            .where(ActivityRecord.activity_type == str(endpoint))
            .order_by(ActivityRecord.id)
        ).all()
        # later rows overwrite earlier -> the latest value per ligand wins
        return {int(row.molecule_id): float(row.value) for row in rows}

    def _artifact_path(self, filename: str) -> Path:
        return self.runtime.get_project_resource_path(RESOURCE_QSAR_MODELS, filename, create_parent=True)

    def _project_root(self) -> Path:
        return Path(self.runtime._require_active_project().path).expanduser().resolve()

    # --- descriptors ----------------------------------------------------------
    def compute_descriptors(
        self,
        *,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        only_missing: bool = True,
        batch_size: int = 1000,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
        compute_fingerprints: bool = False,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
        alerts: bool = False,
    ) -> str:
        """Submit the descriptor job. only_missing=True skips ligands that already have
        descriptors computed (MoleculeRecord.mw populated). With compute_fingerprints=True it
        also stores a Morgan fingerprint per ligand, and only_missing then means "lacks that
        fingerprint" so FPs can be added to already-described ligands. With alerts=True it is
        the alerts job instead: PAINS and identity keys, which a library import leaves out."""
        self.runtime._require_active_project()
        molecule_set_ref = (
            None
            if molecule_set is None or is_molecule_scope(molecule_set)
            else ensure_molecule_set_ref(self.runtime, molecule_set, name="qsar_descriptor_input")
        )
        molecule_scope = scope_payload(molecule_set) if is_molecule_scope(molecule_set) else {}
        filters = apply_workflow_filters(
            {**dict(molecule_scope.get("filters") or {}), "excluded": False},
            workflow=QSAR_WORKFLOW,
            role="ligand",
        )
        params = DescriptorJobParams(
            batch_size=batch_size,
            molecule_set_id=None if molecule_set_ref is None else int(molecule_set_ref.id),
            molecule_filters=filters,
            only_missing=bool(only_missing),
            compute_fingerprints=bool(compute_fingerprints),
            fp_radius=int(fp_radius),
            fp_nbits=int(fp_nbits),
            alerts=bool(alerts),
        )
        return self.runtime.submit_job(
            calculate_molecule_alerts_job if alerts else calculate_molecule_descriptors_job,
            params=params.model_dump(mode="python"),
            executor_name=executor_name,
            depends_on=depends_on,
        )

    def compute_fingerprints(
        self,
        *,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        only_missing: bool = True,
        radius: int = 2,
        nbits: int = 2048,
        batch_size: int = 1000,
        executor_name: str = DEFAULT_LOCAL_CPU_EXECUTOR,
        depends_on: list[str] | None = None,
    ) -> str:
        """Submit a Morgan-fingerprint job (also fills physchem descriptors as a side effect)."""
        return self.compute_descriptors(
            molecule_set=molecule_set,
            only_missing=only_missing,
            batch_size=batch_size,
            executor_name=executor_name,
            depends_on=depends_on,
            compute_fingerprints=True,
            fp_radius=radius,
            fp_nbits=nbits,
        )

    def _fingerprint_matrix(self, session, ligand_ids, *, radius: int, nbits: int) -> dict[int, np.ndarray]:
        """molecule_id -> 0/1 vector unpacked from the stored Morgan bitstring bytes (no RDKit)."""
        fp_type = FingerprintType.ECFP6 if int(radius) >= 3 else FingerprintType.ECFP4
        statement = (
            select(FingerprintRecord)
            .where(FingerprintRecord.fp_type == fp_type)
            .where(FingerprintRecord.nbits == int(nbits))
            .where(FingerprintRecord.radius == int(radius))
        )
        if ligand_ids is not None:
            statement = statement.where(FingerprintRecord.molecule_id.in_([int(i) for i in ligand_ids]))
        out: dict[int, np.ndarray] = {}
        for rec in session.exec(statement):
            out[int(rec.molecule_id)] = np.frombuffer(rec.fp_binary, dtype=np.uint8) - ord("0")
        return out

    def _rdkit2d_matrix(self, session, ligands) -> dict[int, dict[str, float]]:
        """{molecule_id: {descriptor: value}} for the full RDKit 2D block. Reads the cached
        DescriptorBlockRecord(block='rdkit2d') and computes+persists any missing ligand on the fly,
        so the first training pays the cost once and later runs are instant. # ponytail: compute
        happens inside the (already off-GUI) train call; no separate job for v1."""
        from amdockvs.models import DescriptorBlockRecord

        ids = [int(r.id or 0) for r in ligands]
        cached = {
            int(r.molecule_id): dict(r.values_json or {})
            for r in session.exec(
                select(DescriptorBlockRecord)
                .where(DescriptorBlockRecord.block == "rdkit2d")
                .where(DescriptorBlockRecord.molecule_id.in_(ids or [0]))
            )
        }
        to_compute = [r for r in ligands if int(r.id or 0) not in cached]
        if to_compute:
            from amdockvs.chemistry.descriptors import calculate_rdkit2d_descriptors

            for rec in to_compute:
                mol = self._ligand_mol(session, rec)
                if mol is None:
                    continue
                values = calculate_rdkit2d_descriptors(mol)
                cached[int(rec.id or 0)] = values
                session.add(DescriptorBlockRecord(molecule_id=int(rec.id or 0), block="rdkit2d", values_json=values))
            session.commit()
        return {int(r.id or 0): cached[int(r.id or 0)] for r in ligands if int(r.id or 0) in cached}

    def applicability_domain(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        k: int = 5,
        threshold: float = 0.3,
        fp_radius: int = 2,
        fp_nbits: int = 2048,
    ) -> list[dict[str, Any]]:
        """Per-ligand applicability domain by ECFP4 similarity: mean Tanimoto to the k nearest
        *training* ligands of ``model``; in_domain = that mean >= threshold. Answers "is this
        prediction an interpolation or an extrapolation?". Needs Morgan fingerprints computed."""
        self.runtime._require_active_project()
        train_ids = [int(mid) for mid, sub in self.model_subsets(model=model).items() if sub == "train"]
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            if not train_ids:  # model recorded no split → use every labeled training ligand as reference
                record = session.get(QSARModelRecord, int(model.id if hasattr(model, "id") else model))
                train_ids = [int(m) for m in ((record.metrics or {}).get("usable_ligands") or [])] if record else []
            fp_map = self._fingerprint_matrix(
                session, train_ids + [int(r.id or 0) for r in ligands], radius=fp_radius, nbits=fp_nbits
            )
            names = {int(r.id or 0): str(r.name or "") for r in ligands}
        train_fps = [fp_map[i] for i in train_ids if i in fp_map]
        if not train_fps:
            raise ValueError("No training fingerprints - compute Morgan fingerprints (ECFP4) first.")
        train_matrix = np.asarray(train_fps, dtype=float)
        train_norm = train_matrix.sum(axis=1)  # popcount per training fp
        out: list[dict[str, Any]] = []
        for rec in ligands:
            mid = int(rec.id or 0)
            query = fp_map.get(mid)
            if query is None:
                continue
            query = np.asarray(query, dtype=float)
            inter = train_matrix @ query  # |A ∩ B| per training fp
            union = train_norm + query.sum() - inter
            tanimoto = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
            top = np.sort(tanimoto)[::-1][: max(1, int(k))]
            sim = float(np.mean(top))
            out.append({"molecule_id": mid, "name": names.get(mid, ""),
                        "similarity": sim, "in_domain": bool(sim >= float(threshold))})
        return out

    def correlation_matrix(
        self,
        *,
        feature_source: str = "descriptors",
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        corr_threshold: float = 0.95,
    ) -> dict[str, Any]:
        """Pearson correlation matrix across the descriptor block's features (over the project's
        ligands) — the redundancy the training pipeline prunes. feature_source 'descriptors' (14) or
        'rdkit2d' (~200); ecfp4 is unsupported (2048 binary bits). Returns {labels, matrix, dropped}
        where dropped = features the |r|>corr_threshold filter would remove (kept feature listed first)."""
        self.runtime._require_active_project()
        source = str(feature_source).lower()
        if source in {"ecfp4", "fingerprint", "morgan"}:
            raise ValueError("Correlation heatmap is for descriptor blocks, not fingerprints.")
        if source in {"rdkit2d", "rdkit_2d", "rdkit-2d"}:
            feature_kind = "rdkit2d"
            from amdockvs.chemistry.descriptors import rdkit2d_descriptor_names

            features = tuple(rdkit2d_descriptor_names())
        else:
            feature_kind = "descriptors"
            features = normalize_feature_names(None)
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            _ids, x_rows, names, _missing = self._design_matrix(
                session, ligands, feature_kind=feature_kind, features=features, fp_radius=2, fp_nbits=2048
            )
        if len(x_rows) < 2:
            raise ValueError("Need >=2 ligands with descriptors to compute correlations.")
        x_matrix = np.asarray(x_rows, dtype=float)
        # median-impute rdkit2d gaps, then drop zero-variance columns (correlation is undefined there)
        medians = np.nanmedian(x_matrix, axis=0)
        nan_idx = np.where(np.isnan(x_matrix))
        x_matrix[nan_idx] = np.take(medians, nan_idx[1])
        keep = x_matrix.var(axis=0) > 0
        x_matrix = x_matrix[:, keep]
        labels = [n for n, k in zip(names, keep) if k]
        corr = np.nan_to_num(np.corrcoef(x_matrix, rowvar=False), nan=0.0)
        from amdockvs.qsar.modeling import CorrelationFilter

        support = CorrelationFilter(threshold=float(corr_threshold)).fit(x_matrix).get_support()
        dropped = [n for n, kept in zip(labels, support) if not kept]
        return {"labels": labels, "matrix": corr.tolist(), "dropped": dropped, "n_ligands": int(x_matrix.shape[0])}

    def list_descriptors(self) -> list[dict[str, Any]]:
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            rows = session.exec(
                select(MoleculeRecord)
                .where(MoleculeRecord.is_ligand.is_(True))
                .where(MoleculeRecord.mw.is_not(None))
                .order_by(MoleculeRecord.id)
            ).all()
        return [
            {"molecule_id": int(r.id), **{name: getattr(r, name, None) for name in DEFAULT_QSAR_FEATURES}}
            for r in rows
            if r.id is not None
        ]

    # --- models ---------------------------------------------------------------
    @staticmethod
    def supported_algorithms(task: str = "regression") -> tuple[str, ...]:
        return supported_algorithms(task)

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
        split balances the labels — not just chemical space. Scaffold split keeps whole scaffolds
        together but does NOT stratify by class, so an imbalanced endpoint can land most actives on
        one side. Returns {task, categories, train[], test[], n_train, n_test, note} — counts per
        class (classification) or per value-bin (regression), aligned to `categories`."""
        self.runtime._require_active_project()
        from amdockvs.qsar.modeling import grouped_holdout_split

        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("split_preview requires endpoint.")
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            activity_by_ligand = self._latest_activity_by_ligand(session, endpoint_text)
            labeled = [r for r in ligands if int(r.id or 0) in activity_by_ligand]
            used_ids = [int(r.id or 0) for r in labeled]
            scaffolds = self._scaffold_keys(labeled) if split == "scaffold" else {}
        if len(used_ids) < 2:
            raise ValueError(f"Only {len(used_ids)} labeled ligand(s) for '{endpoint_text}'.")
        y = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
        if split == "scaffold":
            train_idx, test_idx = grouped_holdout_split(
                [scaffolds.get(mid, "") for mid in used_ids], test_size=float(test_size), seed=int(seed)
            )
        else:
            from sklearn.model_selection import train_test_split

            train_idx, test_idx = train_test_split(
                list(range(len(used_ids))), test_size=float(test_size), random_state=int(seed)
            )
        kind = self.endpoint_kinds().get(endpoint_text, "")
        is_class = kind == "categorical" or class_threshold is not None or set(np.unique(y).tolist()) <= {0.0, 1.0}
        if is_class:
            yb = (y >= float(class_threshold)).astype(int) if class_threshold is not None else y.astype(int)
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
        note = f"mean value - train {float(y[train_idx].mean()):.3f} vs test {float(y[test_idx].mean()):.3f}"
        return {"task": "regression", "categories": cats, "train": train, "test": test,
                "n_train": len(train_idx), "n_test": len(test_idx), "note": note}

    def train(
        self,
        *,
        endpoint: str,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        algorithm: str = "linear_regression",
        task: str = "regression",
        feature_names: Sequence[str] | None = None,
        feature_source: str = "descriptors",
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
    ) -> QSARModelRecord:
        """Fit a model template from the ligands of ``molecule_set`` that have an activity for
        ``endpoint``. feature_source: 'descriptors' (14 physchem columns), 'rdkit2d' (full ~200
        RDKit block, computed+cached on first use) or 'ecfp4' (stored Morgan fingerprint). For
        descriptor sources the preprocessing (impute→drop zero-variance→drop |r|>corr_threshold→
        scale) lives inside the CV pipeline, so scores are leakage-free. split 'scaffold' holds out
        whole Bemis-Murcko scaffolds, including within CV. cv_folds>=2 adds a training-only CV
        score (Q²/MCC); label-permutation checks also use only training molecules. class_threshold binarizes
        a *continuous* endpoint (active = value >= threshold); leave None for categorical endpoints.
        y_scramble>=1 runs that many label-permutation refits (should score ~0; else signal is spurious)."""
        self.runtime._require_active_project()
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("train requires endpoint.")
        if split not in {"random", "scaffold"}:
            raise ValueError("split must be 'random' or 'scaffold'.")
        if not 0 <= float(test_size) < 1:
            raise ValueError("test_size must be >=0 and <1; use 0 to disable the holdout.")
        source = str(feature_source).lower()
        if source in {"ecfp4", "fingerprint", "morgan"}:
            feature_kind = "ecfp4"
        elif source in {"rdkit2d", "rdkit_2d", "rdkit-2d"}:
            feature_kind = "rdkit2d"
        else:
            feature_kind = "descriptors"
        if feature_kind == "descriptors":
            features = normalize_feature_names(feature_names)
        elif feature_kind == "rdkit2d":
            from amdockvs.chemistry.descriptors import rdkit2d_descriptor_names

            features = tuple(rdkit2d_descriptor_names())
        else:
            features = ()
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            activity_by_ligand = self._latest_activity_by_ligand(session, endpoint_text)
            labeled = [r for r in ligands if int(r.id or 0) in activity_by_ligand]
            missing_act = len(ligands) - len(labeled)
            used_ids, x_rows, out_names, missing_desc = self._design_matrix(
                session, labeled, feature_kind=feature_kind, features=features, fp_radius=fp_radius, fp_nbits=fp_nbits
            )
            scaffolds = self._scaffold_keys(labeled) if split == "scaffold" else {}
        y_rows = [activity_by_ligand[mid] for mid in used_ids]
        if len(x_rows) < 2:
            raise ValueError(
                f"train needs >=2 usable ligands; got {len(x_rows)} "
                f"(missing_descriptor={missing_desc}, missing_activity={missing_act})."
            )

        x_matrix = np.asarray(x_rows, dtype=float)
        y_raw = np.asarray(y_rows, dtype=float)
        if task == "classification":
            y_vector = (y_raw >= float(class_threshold)).astype(int) if class_threshold is not None else y_raw.astype(int)
        else:
            y_vector = y_raw

        if task == "classification" and np.unique(y_vector).size < 2:
            raise ValueError(
                "All ligands fall in a single class after labeling - a model can't be trained. "
                "For a categorical endpoint don't set a class threshold (the value is already the "
                "label); for a continuous one, pick a threshold that leaves both classes populated."
            )

        x_train, y_train, holdout = x_matrix, y_vector, None
        train_indices = np.arange(len(used_ids))
        test_ids: set[int] = set()  # for split-membership reporting (train/test; no separate val set)
        if float(test_size) > 0:
            if split == "scaffold":
                from amdockvs.qsar.modeling import grouped_holdout_split

                train_idx, test_idx = grouped_holdout_split(
                    [scaffolds.get(mid, "") for mid in used_ids], test_size=float(test_size), seed=int(seed)
                )
                if test_idx and train_idx:
                    train_indices = np.asarray(train_idx)
                    x_train, y_train = x_matrix[train_idx], y_vector[train_idx]
                    holdout = (x_matrix[test_idx], y_vector[test_idx])
                    test_ids = {used_ids[i] for i in test_idx}
            else:
                from sklearn.model_selection import train_test_split

                indices = np.arange(x_matrix.shape[0])
                train_i, test_i = train_test_split(indices, test_size=float(test_size), random_state=int(seed))
                train_indices = train_i
                x_train, y_train = x_matrix[train_i], y_vector[train_i]
                holdout = (x_matrix[test_i], y_vector[test_i])
                test_ids = {used_ids[i] for i in test_i}

            if holdout is None or len(y_train) < 2:
                raise ValueError(
                    "The requested holdout cannot leave at least two training molecules and "
                    "one test molecule. Add more molecules/scaffolds or reduce the test fraction."
                )
            if task == "classification" and np.unique(y_train).size < 2:
                raise ValueError(
                    "The requested split leaves only one class in training. Add more labeled "
                    "molecules/scaffolds or choose a different split or seed."
                )

        training_groups = (
            np.asarray([scaffolds.get(used_ids[int(i)], "") for i in train_indices])
            if split == "scaffold" else None
        )

        fitted = fit_model(
            x_train, y_train, feature_names=out_names, algorithm=algorithm, task=task, hyperparams=hyperparams,
            feature_kind=feature_kind, corr_threshold=float(corr_threshold),
            fp_radius=fp_radius if feature_kind == "ecfp4" else None,
            fp_nbits=fp_nbits if feature_kind == "ecfp4" else None,
        )
        feature_summary = {
            "descriptors": list(out_names),
            "rdkit2d": f"rdkit2d:{len(out_names)}",
            "ecfp4": f"ecfp4:{fp_nbits}",
        }[feature_kind]
        metrics: dict[str, Any] = {
            "task": task,
            "algorithm": algorithm,
            "feature_kind": feature_kind,
            "features": feature_summary,
            "split": split,
            "class_threshold": class_threshold,
            "positive_class": int(max(fitted.classes)) if len(fitted.classes) == 2 else None,
            "corr_threshold": float(corr_threshold),
            "n_train": int(x_train.shape[0]),
            "train": self._score(task, y_train, fitted, x_train),
            "usable_ligands": used_ids,
            "split_assignment": [[mid, "test" if mid in test_ids else "train"] for mid in used_ids],
        }
        if holdout is not None:
            metrics["test"] = self._score(task, holdout[1], fitted, holdout[0])
        if cv_folds and int(cv_folds) >= 2:
            from amdockvs.qsar.modeling import cross_validation_metrics

            metrics["cv_folds"] = int(cv_folds)
            metrics["cv_samples"] = int(x_train.shape[0])
            metrics["cv_split"] = "scaffold" if training_groups is not None else "row"
            cv_metrics = cross_validation_metrics(
                x_train, y_train, algorithm=algorithm, task=task, cv=int(cv_folds),
                hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=float(corr_threshold),
                groups=training_groups,
            )
            metrics.update(cv_metrics)
            if task == "regression":
                metrics["q2_method"] = "pooled_oof"
            cv_score = cv_metrics["q2" if task == "regression" else "cv_mcc"]
            if cv_score is None:
                if training_groups is not None and len(np.unique(training_groups)) < int(cv_folds):
                    metrics["cv_warning"] = (
                        f"CV not computed: requested {int(cv_folds)} folds, but the training set "
                        f"contains only {len(np.unique(training_groups))} scaffold group(s)."
                    )
                else:
                    metrics["cv_warning"] = (
                        "CV score undefined: insufficient training samples, a constant response, or unscorable folds."
                    )
        if y_scramble and int(y_scramble) >= 1:
            from amdockvs.qsar.modeling import y_scramble_score

            scramble_folds = int(cv_folds) if int(cv_folds) >= 2 else 3
            metrics["y_scramble_samples"] = int(x_train.shape[0])
            metrics["y_scramble_cv_folds"] = scramble_folds
            metrics["y_scramble_metric"] = "q2_pooled_oof" if task == "regression" else "cv_mcc"
            metrics["y_scramble"] = y_scramble_score(
                x_train, y_train, algorithm=algorithm, task=task, n_permutations=int(y_scramble),
                hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=float(corr_threshold), seed=int(seed),
                cv=scramble_folds, groups=training_groups,
            )
            if metrics["y_scramble"] is None:
                metrics["y_scramble_warning"] = (
                    "Label-permutation validation not computed: insufficient training samples "
                    "or scaffold groups, or unscorable folds."
                )
        importance = feature_importance(fitted.estimator, out_names)
        if importance:
            metrics["feature_importance"] = [[n, v] for n, v in importance]

        with self.runtime.workspace.project_db.get_session() as session:
            model_record = QSARModelRecord(
                name=str(name or "").strip() or f"{algorithm}_{endpoint_text}",
                algorithm=algorithm,
                target=endpoint_text,
                feature_type="fingerprints_ecfp4" if feature_kind == "ecfp4" else "descriptors_2d",
                source="trained",
                metrics=metrics,
            )
            session.add(model_record)
            session.flush()
            session.refresh(model_record)
            artifact = self._artifact_path(f"model_{int(model_record.id or 0)}.joblib")
            save_model(artifact, fitted)
            model_record.model_path = str(artifact.relative_to(self._project_root()))
            session.add(model_record)
            session.commit()
            session.refresh(model_record)
            return model_record

    def load_external_model(
        self,
        *,
        path: PathLike,
        name: str,
        endpoint: str = "",
    ) -> QSARModelRecord:
        """Register a pre-built model artifact (a joblib FittedModel) for prediction. No
        training input needed — the artifact already carries features + task."""
        self.runtime._require_active_project()
        source_path = Path(path).expanduser().resolve()
        fitted = load_model(source_path)  # validates format
        with self.runtime.workspace.project_db.get_session() as session:
            model_record = QSARModelRecord(
                name=str(name or source_path.stem),
                algorithm=fitted.algorithm,
                target=str(endpoint or ""),
                feature_type="fingerprints_ecfp4" if fitted.feature_kind == "ecfp4" else "descriptors_2d",
                source="external",
                metrics={"task": fitted.task, "features": list(fitted.feature_names),
                         "feature_kind": fitted.feature_kind,
                         "positive_class": int(max(fitted.classes)) if len(fitted.classes) == 2 else None},
            )
            session.add(model_record)
            session.flush()
            session.refresh(model_record)
            artifact = self._artifact_path(f"model_{int(model_record.id or 0)}.joblib")
            save_model(artifact, fitted)
            model_record.model_path = str(artifact.relative_to(self._project_root()))
            session.add(model_record)
            session.commit()
            session.refresh(model_record)
            return model_record

    @staticmethod
    def _filter_by_subset(ligands, record, subset: str):
        """Keep only the ligands in a model's recorded train/test split. subset 'all' passes
        everything; 'train'/'test' keep the matching ids (empty split → nothing to filter on)."""
        mode = str(subset or "all").lower()
        if mode not in {"all", "train", "test"}:
            raise ValueError("subset must be 'all', 'train', or 'test'.")
        if mode not in {"train", "test"}:
            return ligands
        assign = {int(mid): str(sub) for mid, sub in (record.metrics or {}).get("split_assignment", [])}
        return [r for r in ligands if assign.get(int(r.id or 0)) == mode]

    @staticmethod
    def _features_for(fitted: FittedModel) -> tuple[str, ...]:
        """The feature-name list the design matrix needs for a fitted model. ecfp4 uses none;
        rdkit2d uses the stored names as-is (they aren't in the 14-name physchem whitelist, so
        normalize_feature_names would reject them); descriptors are validated/normalised."""
        if fitted.feature_kind == "ecfp4":
            return ()
        if fitted.feature_kind == "rdkit2d":
            return tuple(fitted.feature_names)
        return normalize_feature_names(fitted.feature_names)

    def _diagnostic_data(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        endpoint: str | None = None,
        subset: str = "all",
    ) -> tuple[FittedModel, np.ndarray, np.ndarray, dict[str, Any]]:
        """Read labeled features from exactly the requested evaluation subset."""
        self.runtime._require_active_project()
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            endpoint_text = str(endpoint or record.target or "").strip()
            activity_by_ligand = self._latest_activity_by_ligand(session, endpoint_text)
            threshold = (record.metrics or {}).get("class_threshold")
            labeled = [r for r in ligands if int(r.id or 0) in activity_by_ligand]
            labeled = self._filter_by_subset(labeled, record, subset)
            used_ids, x_rows, _n, _m = self._design_matrix(
                session, labeled, feature_kind=fitted.feature_kind, features=self._features_for(fitted),
                fp_radius=int(fitted.fp_radius or 2), fp_nbits=int(fitted.fp_nbits or 2048),
            )
        if not x_rows:
            raise ValueError(f"No labeled, feature-complete ligands in the '{subset}' subset.")
        x_matrix = np.asarray(x_rows, dtype=float)
        y_raw = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
        y_true = y_raw
        if fitted.task == "classification":
            y_true = (y_raw >= float(threshold)).astype(int) if threshold is not None else y_raw.astype(int)
        return fitted, x_matrix, y_true, {
            "endpoint": endpoint_text, "subset": str(subset or "all").lower(), "n": len(y_true),
        }

    def observed_predicted_points(self, *, model, molecule_set=None, endpoint=None, subset="all") -> dict[str, Any]:
        fitted, x, y_true, result = self._diagnostic_data(
            model=model, molecule_set=molecule_set, endpoint=endpoint, subset=subset)
        if fitted.task != "regression":
            raise ValueError("Observed vs predicted is only for regression models.")
        return {**result, "points": list(zip(y_true.tolist(), fitted.predict(x).tolist()))}

    def _binary_diagnostic_data(self, **kwargs):
        fitted, x, y_true, result = self._diagnostic_data(**kwargs)
        classes = np.asarray(getattr(fitted.estimator, "classes_", []))
        if fitted.task != "classification" or len(classes) != 2:
            raise ValueError("This diagnostic requires a binary classification model.")
        classes = np.sort(classes)
        if not np.isin(y_true, classes).all():
            raise ValueError("The selected endpoint contains labels outside the model's trained classes.")
        return fitted, x, y_true, classes, result

    def _binary_curve_data(self, **kwargs):
        fitted, x, y_true, classes, result = self._binary_diagnostic_data(**kwargs)
        if len(np.unique(y_true)) != 2:
            raise ValueError("ROC and precision-recall curves need both classes in the selected subset.")
        scores = fitted.predict_positive_probability(x)
        if scores is None:
            raise ValueError("ROC and precision-recall curves require a model with binary probabilities.")
        positive = classes[-1].item()
        return (y_true == positive).astype(int), scores, {**result, "positive_class": positive}

    def roc_curve_points(self, *, model, molecule_set=None, endpoint=None, subset="all") -> dict[str, Any]:
        """Binary ROC using positive-class probabilities on the requested subset."""
        y_true, scores, result = self._binary_curve_data(
            model=model, molecule_set=molecule_set, endpoint=endpoint, subset=subset)
        from sklearn.metrics import roc_auc_score, roc_curve

        fpr, tpr, _thr = roc_curve(y_true, scores)
        return {
            **result,
            "points": [(float(a), float(b)) for a, b in zip(fpr, tpr)],
            "auc": float(roc_auc_score(y_true, scores)),
        }

    def precision_recall_curve_points(self, *, model, molecule_set=None, endpoint=None, subset="all") -> dict[str, Any]:
        """Precision/recall, average precision (AP), and the prevalence baseline."""
        from sklearn.metrics import average_precision_score, precision_recall_curve

        y_true, scores, result = self._binary_curve_data(
            model=model, molecule_set=molecule_set, endpoint=endpoint, subset=subset)
        precision, recall, _thresholds = precision_recall_curve(y_true, scores)
        return {**result, "points": list(zip(recall.tolist(), precision.tolist())),
                "average_precision": float(average_precision_score(y_true, scores)),
                "prevalence": float(np.mean(y_true))}

    def confusion_matrix_data(self, *, model, molecule_set=None, endpoint=None, subset="all") -> dict[str, Any]:
        """Raw counts: rows are actual classes and columns are predicted classes."""
        from sklearn.metrics import confusion_matrix

        fitted, x, y_true, classes, result = self._binary_diagnostic_data(
            model=model, molecule_set=molecule_set, endpoint=endpoint, subset=subset)
        counts = confusion_matrix(y_true, fitted.predict(x), labels=classes)
        return {**result, "labels": classes.tolist(), "counts": counts.tolist(),
                "positive_class": classes[-1].item()}

    def _load_fitted(self, session, model: QSARModelRef | int) -> tuple[QSARModelRecord, FittedModel]:
        model_id = int(model.id if hasattr(model, "id") else model)
        record = session.get(QSARModelRecord, model_id)
        if record is None:
            raise ValueError(f"Unknown QSAR model id: {model_id}")
        fitted = load_model(self._project_root() / record.model_path)
        return record, fitted

    def list_models(self) -> list[QSARModelRecord]:
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            return list(session.exec(select(QSARModelRecord).order_by(QSARModelRecord.id)))

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
        model_id = int(model.id if hasattr(model, "id") else model)
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

    def shard_predictions(self, *, model: QSARModelRef | int):
        """A model's predictions over the library's shards, as a frame (key, value, confidence).
        Only the active generation's shards are read: an older one is no longer the library.

        ponytail: the whole frame in RAM (~24 B/molecule, 2.4 GB at 10^8); a per-file top-N
        before the concat when a library gets there."""
        import pandas as pd

        self.runtime._require_active_project()
        directory = self._shard_predictions_dir(int(model.id if hasattr(model, "id") else model))
        paths = [directory / f"{int(row['id'])}.parquet"
                 for row in ShardStore(self.runtime.workspace.project_db).iter_rows(batch_size=1000)]
        frames = [pd.read_parquet(path) for path in paths if path.is_file()]
        if not frames:
            return pd.DataFrame({"key": pd.Series(dtype="int64"), "value": pd.Series(dtype=float),
                                 "confidence": pd.Series(dtype=float)})
        return pd.concat(frames, ignore_index=True)

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
        model_id = int(model.id if hasattr(model, "id") else model)
        frame = self.shard_predictions(model=model_id)
        scored = len(frame)
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, model_id)
            if record is None:
                raise ValueError(f"Unknown QSAR model id: {model_id}")
            classification = (record.metrics or {}).get("task") == "classification"
        if "positive_probability" not in frame:
            frame["positive_probability"] = np.nan  # older regression shards remain usable
        score = "positive_probability" if classification else "value"
        if classification and frame[score].isna().any():
            raise ValueError(
                "Classifier ranking requires binary positive-class probabilities. "
                "Run Predict shards again to update older predictions."
            )
        if cutoff is not None:
            frame = frame[frame[score] >= float(cutoff) if higher_is_better else frame[score] <= float(cutoff)]
        frame = frame.sort_values([score, "key"], ascending=[not higher_is_better, True])
        if top_n is not None:
            frame = frame.head(max(0, int(top_n)))
        kept = list(frame[["key", "value", "confidence", "positive_probability"]].itertuples(index=False, name=None))
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
                )
                for key, value, confidence, probability in kept if key in promoted
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
                    QSARPredictionRecord.model_id == int(model.id if hasattr(model, "id") else model)
                )
            if ligand_ids is not None:
                statement = statement.where(QSARPredictionRecord.molecule_id.in_([int(i) for i in ligand_ids]))
            return list(session.exec(statement.order_by(QSARPredictionRecord.id)))

    def prediction_rows(self, *, model: QSARModelRef | int) -> list[dict[str, Any]]:
        """Predictions joined with the ligand name, for the Predictions table:
        [{molecule_id, name, value, confidence}] ordered by ligand id. Excluded ligands are left
        out, as in every other ligand listing."""
        self.runtime._require_active_project()
        model_id = int(model.id if hasattr(model, "id") else model)
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
             "positive_probability": None if r.positive_probability is None else float(r.positive_probability)}
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
        label or max-class confidence. Older classifier predictions must be regenerated first.

        The cut is a subquery over the predictions, so nothing here grows with the library: the
        exclusion is one UPDATE and only ``ids_limit`` ids ever come back. Returns {"kept": n,
        "rest": n, "excluded": how many were marked, "ids": the first ``ids_limit`` kept, best first}."""
        if top_n is None and cutoff is None:
            raise ValueError("cut_predictions needs top_n and/or cutoff.")
        self.runtime._require_active_project()
        model_id = int(model.id if hasattr(model, "id") else model)
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, model_id)
            if record is None:
                raise ValueError(f"Unknown QSAR model id: {model_id}")
            classification = (record.metrics or {}).get("task") == "classification"
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
                    "Classifier ranking requires positive-class probabilities from a binary model. "
                    "Run prediction again to update older predictions; multiclass or "
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

    def evaluate(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        endpoint: str | None = None,
        subset: str = "all",
    ) -> dict[str, Any]:
        """Score labeled molecules in 'all', 'train', or 'test' (recorded model split).

        A requested subset with no usable molecules raises instead of scoring all molecules.
        """
        self.runtime._require_active_project()
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            endpoint_text = str(endpoint or record.target or "").strip()
            if not endpoint_text:
                raise ValueError("evaluate needs an endpoint (model has no target).")
            activity_by_ligand = self._latest_activity_by_ligand(session, endpoint_text)
            features = self._features_for(fitted)
            threshold = (record.metrics or {}).get("class_threshold")
            labeled = [r for r in ligands if int(r.id or 0) in activity_by_ligand]
            labeled = self._filter_by_subset(labeled, record, subset)
            used_ids, x_rows, _names, _missing = self._design_matrix(
                session, labeled, feature_kind=fitted.feature_kind, features=features,
                fp_radius=int(fitted.fp_radius or 2), fp_nbits=int(fitted.fp_nbits or 2048),
            )
            if not x_rows:
                raise ValueError(f"No labeled, feature-complete ligands in the '{subset}' subset to evaluate.")
            x_matrix = np.asarray(x_rows, dtype=float)
            y_raw = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
            if fitted.task == "classification":
                y_eval = (y_raw >= float(threshold)).astype(int) if threshold is not None else y_raw.astype(int)
                metrics = self._score("classification", y_eval, fitted, x_matrix)
            else:
                metrics = self._score("regression", y_raw, fitted, x_matrix)
            metrics["model_id"] = int(record.id or 0)
            metrics["endpoint"] = endpoint_text
            metrics["subset"] = str(subset or "all").lower()
            return metrics

    def model_subsets(self, *, model: QSARModelRef | int) -> dict[int, str]:
        """{ligand_id: 'train'|'test'} for a trained model's split (empty if not recorded).
        The lean pipeline has no separate validation set — only train/test."""
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, int(model.id if hasattr(model, "id") else model))
            if record is None:
                return {}
            return {int(mid): str(sub) for mid, sub in (record.metrics or {}).get("split_assignment", [])}

    def _ligand_mol(self, session, rec):
        """An RDKit mol for a ligand: from its stored structure, else its canonical SMILES."""
        from rdkit import Chem

        from amdockvs.chemistry.structures import molecule_mol

        mol = molecule_mol(self.runtime.workspace.project_db, rec, remove_hs=True)
        if mol is not None:
            return mol
        smiles = session.exec(
            select(MoleculeRepresentation.value)
            .where(MoleculeRepresentation.molecule_id == rec.id)
            .where(MoleculeRepresentation.repr_type == ReprType.SMILES_CANONICAL)
        ).first()
        return Chem.MolFromSmiles(smiles) if smiles else None

    def atom_contributions(self, *, model: QSARModelRef | int, ligand_id: int) -> dict[str, Any]:
        """Per-atom contribution to an ECFP4 model's prediction (StarDrop's "glowing molecule").
        Returns {molblock, weights[per atom], prediction}. ECFP4 models only."""
        self.runtime._require_active_project()
        from rdkit import Chem, DataStructs
        from rdkit.Chem.Draw import SimilarityMaps

        with self.runtime.workspace.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            if fitted.feature_kind != "ecfp4":
                raise ValueError("Glowing molecule needs an ECFP4 (fingerprint) model.")
            rec = session.get(MoleculeRecord, int(ligand_id))
            if rec is None:
                raise ValueError(f"Unknown ligand id: {ligand_id}")
            mol = self._ligand_mol(session, rec)
        if mol is None:
            raise ValueError("No structure available for this ligand.")
        radius, nbits = int(fitted.fp_radius or 2), int(fitted.fp_nbits or 2048)

        def fp_fn(probe, atom_id=-1):
            return SimilarityMaps.GetMorganFingerprint(probe, atomId=atom_id, radius=radius, nBits=nbits)

        # A classifier's label does not move when one atom is removed, so every weight would be
        # 0: follow the probability instead — P(active) when binary, else P(predicted class).
        proba = getattr(fitted.estimator, "predict_proba", None) if fitted.task == "classification" else None
        column = -1

        def row(fp):
            arr = np.zeros((nbits,), dtype=np.float64)
            DataStructs.ConvertToNumpyArray(fp, arr)
            return arr.reshape(1, -1)

        def pred_fn(fp):
            if proba is None:
                return float(fitted.predict(row(fp))[0])
            return float(proba(row(fp))[0][column])

        if proba is not None and len(fitted.classes) > 2:
            column = int(np.argmax(proba(row(fp_fn(mol)))[0]))
        weights = SimilarityMaps.GetAtomicWeightsForModel(mol, fp_fn, pred_fn)
        return {
            "molblock": Chem.MolToMolBlock(mol),
            "weights": [float(w) for w in weights],
            "prediction": pred_fn(fp_fn(mol)),
        }


__all__ = ["QSARAPI"]
