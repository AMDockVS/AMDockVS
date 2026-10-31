"""What a model is worth: its diagnostics, read from the molecules it recorded at training
(``qsar_model_members``), plus re-scoring on new molecules, applicability domain and atom weights."""
from __future__ import annotations

from typing import Any

import numpy as np
from sqlmodel import select

from amdockvs.models import MoleculeRecord, QSARModelMemberRecord, QSARModelRecord
from amdockvs.molecules.scopes import MoleculeScope
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef

SUBSETS = ("all", "train", "test", "cv")


class ModelAnalysisMixin:
    def list_models(self) -> list[QSARModelRecord]:
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            return list(session.exec(select(QSARModelRecord).order_by(QSARModelRecord.id)))

    def model_subsets(self, *, model: QSARModelRef | int) -> dict[int, str]:
        """{ligand_id: 'train'|'test'} for the molecules a model was trained/tested on (empty for an
        external model)."""
        self.runtime._require_active_project()
        with self.runtime.workspace.project_db.get_session() as session:
            rows = session.exec(
                select(QSARModelMemberRecord.molecule_id, QSARModelMemberRecord.role)
                .where(QSARModelMemberRecord.model_id == self._model_id(model))
            ).all()
        return {int(mid): str(role) for mid, role in rows}

    def model_members(self, *, model: QSARModelRef | int, subset: str = "all") -> tuple[QSARModelRecord, list]:
        """(model record, its members in ``subset``): 'all', 'train', 'test', or 'cv' — the training
        molecules with an out-of-fold prediction. Raises when the subset is empty."""
        mode = str(subset or "all").lower()
        if mode not in SUBSETS:
            raise ValueError(f"subset must be one of {', '.join(SUBSETS)}.")
        self.runtime._require_active_project()
        member = QSARModelMemberRecord
        with self.runtime.workspace.project_db.get_session() as session:
            record = session.get(QSARModelRecord, self._model_id(model))
            if record is None:
                raise ValueError(f"Unknown QSAR model id: {self._model_id(model)}")
            statement = select(member).where(member.model_id == record.id).order_by(member.molecule_id)
            if mode in {"train", "test"}:
                statement = statement.where(member.role == mode)
            elif mode == "cv":
                statement = statement.where(member.role == "train", member.oof_predicted.is_not(None))
            rows = list(session.exec(statement))
        if not rows:
            raise ValueError(f"No molecules in the '{mode}' subset of model #{record.id}.")
        return record, rows

    def member_table(self, *, model: QSARModelRef | int) -> list[dict[str, Any]]:
        """One row per molecule the model was built on: molecule_id, name, role ('train'|'test'),
        observed, predicted, oof_predicted (a training molecule's, when CV ran), probability."""
        record, rows = self.model_members(model=model)
        with self.runtime.workspace.project_db.get_session() as session:
            names = dict(session.exec(
                select(MoleculeRecord.id, MoleculeRecord.name)
                .where(MoleculeRecord.id.in_([int(r.molecule_id) for r in rows]))
            ).all())
        return [{
            "molecule_id": int(r.molecule_id), "name": str(names.get(r.molecule_id) or ""), "role": r.role,
            "observed": r.observed, "predicted": r.predicted, "oof_predicted": r.oof_predicted,
            "probability": r.probability,
        } for r in rows]

    def _diagnostic_data(self, *, model, subset: str = "all"):
        """(record, observed, predicted, probability, summary) for the subset; 'cv' reads the
        out-of-fold columns. probability is None when any row lacks one."""
        record, rows = self.model_members(model=model, subset=subset)
        oof = str(subset or "all").lower() == "cv"
        observed = np.asarray([r.observed for r in rows], dtype=float)
        predicted = np.asarray([r.oof_predicted if oof else r.predicted for r in rows], dtype=float)
        scores = [r.oof_probability if oof else r.probability for r in rows]
        probability = None if any(p is None for p in scores) else np.asarray(scores, dtype=float)
        return record, observed, predicted, probability, {
            "endpoint": record.target, "subset": str(subset or "all").lower(), "n": len(rows),
            "ids": [int(r.molecule_id) for r in rows],
        }

    def observed_predicted_points(self, *, model, subset="all") -> dict[str, Any]:
        record, observed, predicted, _p, result = self._diagnostic_data(model=model, subset=subset)
        if record.task != "regression":
            raise ValueError("Observed vs predicted is only for regression models.")
        return {**result, "points": list(zip(observed.tolist(), predicted.tolist()))}

    def _binary_diagnostic_data(self, *, model, subset="all"):
        record, observed, predicted, probability, result = self._diagnostic_data(model=model, subset=subset)
        classes = sorted((record.metrics or {}).get("classes") or ())
        if record.task != "classification" or len(classes) != 2:
            raise ValueError("This diagnostic requires a binary classification model.")
        return observed.astype(int), predicted.astype(int), probability, classes, result

    def _binary_curve_data(self, *, model, subset="all"):
        observed, _predicted, probability, classes, result = self._binary_diagnostic_data(model=model, subset=subset)
        if len(np.unique(observed)) != 2:
            raise ValueError("ROC and precision-recall curves need both classes in the selected subset.")
        if probability is None:
            raise ValueError("ROC and precision-recall curves require a model with binary probabilities.")
        positive = classes[-1]
        return (observed == positive).astype(int), probability, {**result, "positive_class": positive}

    def roc_curve_points(self, *, model, subset="all") -> dict[str, Any]:
        """Binary ROC using positive-class probabilities on the requested subset."""
        from sklearn.metrics import roc_auc_score, roc_curve

        y_true, scores, result = self._binary_curve_data(model=model, subset=subset)
        fpr, tpr, _thr = roc_curve(y_true, scores)
        return {
            **result,
            "points": [(float(a), float(b)) for a, b in zip(fpr, tpr)],
            "auc": float(roc_auc_score(y_true, scores)),
        }

    def precision_recall_curve_points(self, *, model, subset="all") -> dict[str, Any]:
        """Precision/recall, average precision (AP), and the prevalence baseline."""
        from sklearn.metrics import average_precision_score, precision_recall_curve

        y_true, scores, result = self._binary_curve_data(model=model, subset=subset)
        precision, recall, _thresholds = precision_recall_curve(y_true, scores)
        return {**result, "points": list(zip(recall.tolist(), precision.tolist())),
                "average_precision": float(average_precision_score(y_true, scores)),
                "prevalence": float(np.mean(y_true))}

    def confusion_matrix_data(self, *, model, subset="all") -> dict[str, Any]:
        """Raw counts: rows are actual classes and columns are predicted classes."""
        observed, predicted, _p, classes, result = self._binary_diagnostic_data(model=model, subset=subset)
        index = {label: i for i, label in enumerate(classes)}
        counts = np.zeros((len(classes), len(classes)), dtype=int)
        for actual, guess in zip(observed.tolist(), predicted.tolist()):
            counts[index[actual], index[guess]] += 1
        return {**result, "labels": list(classes), "counts": counts.tolist(), "positive_class": classes[-1]}

    def evaluate(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        endpoint: str | None = None,
        subset: str = "all",
    ) -> dict[str, Any]:
        """Predict the labeled molecules of ``molecule_set`` again and score them, restricted to the
        model's 'train' or 'test' members, or 'all'. For molecules the model never saw (an external
        set, new activities) — the recorded diagnostics need no re-prediction.

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
            mode = str(subset or "all").lower()
            if mode not in {"all", "train", "test"}:
                raise ValueError("subset must be 'all', 'train', or 'test'.")
            if mode != "all":
                roles = self.model_subsets(model=record.id)
                labeled = [r for r in labeled if roles.get(int(r.id or 0)) == mode]
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
        if not train_ids:
            raise ValueError("This model recorded no training molecules (an external model?): no domain to measure.")
        ligands = self._ligands(molecule_set)
        with self.runtime.workspace.project_db.get_session() as session:
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

    def bit_fragments(self, *, model: QSARModelRef | int, bits, max_molecules: int = 500) -> dict[str, str]:
        """{"bit_<i>": SMILES of the substructure that sets it} for an ECFP4 model, read off the
        first of its training molecules that has the bit (Morgan bitInfo). A bit no molecule sets,
        or a collision, keeps whichever environment came first."""
        from rdkit import Chem
        from rdkit.Chem import rdFingerprintGenerator

        self.runtime._require_active_project()
        wanted = {int(str(b).removeprefix("bit_")) for b in bits}
        with self.runtime.workspace.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            if fitted.feature_kind != "ecfp4":
                raise ValueError("Bit fragments are for ECFP4 models.")
            members = session.exec(
                select(QSARModelMemberRecord.molecule_id).where(QSARModelMemberRecord.model_id == record.id)
                .limit(int(max_molecules))
            ).all()
            generator = rdFingerprintGenerator.GetMorganGenerator(
                radius=int(fitted.fp_radius or 2), fpSize=int(fitted.fp_nbits or 2048), includeChirality=True)
            found: dict[str, str] = {}
            for molecule_id in members:
                rec = session.get(MoleculeRecord, int(molecule_id))
                mol = None if rec is None else self._ligand_mol(session, rec)
                if mol is None:
                    continue
                output = rdFingerprintGenerator.AdditionalOutput()
                output.AllocateBitInfoMap()
                generator.GetFingerprint(mol, additionalOutput=output)
                for bit, environments in output.GetBitInfoMap().items():
                    if bit not in wanted or f"bit_{bit}" in found:
                        continue
                    atom, radius = environments[0]
                    bonds = list(Chem.FindAtomEnvironmentOfRadiusN(mol, int(radius), int(atom)))
                    atoms = {int(atom)} | {a for b in bonds for a in (mol.GetBondWithIdx(b).GetBeginAtomIdx(),
                                                                      mol.GetBondWithIdx(b).GetEndAtomIdx())}
                    found[f"bit_{bit}"] = Chem.MolFragmentToSmiles(
                        mol, atomsToUse=sorted(atoms), bondsToUse=bonds or None, canonical=True)
                if len(found) == len(wanted):
                    break
        return found

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
