"""QSAR API: activities, features, models and what they predict. Standalone in script/notebook mode;
the UI just calls these methods.

``QSARAPI`` is one object made of one mixin per concern, so ``runtime.qsar.<method>`` stays flat:
``build`` (splits, training, registering models), ``predict`` (scoring jobs), ``model_analysis``
(what a model is worth, read from its recorded members) and ``results`` (reading and cutting its
predictions). The helpers they share live here. scikit-learn is optional and imported only where a
model is fit or loaded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sqlmodel import select

from amdockvs.core.constants import DEFAULT_LOCAL_CPU_EXECUTOR, RESOURCE_QSAR_MODELS
from amdockvs.core.vocab import ReprType
from amdockvs.models import (
    ActivityRecord,
    FingerprintRecord,
    MoleculeRecord,
    MoleculeRepresentation,
    QSARModelRecord,
)
from amdockvs.models.descriptors import FingerprintType
from amdockvs.qsar.activities import ActivitiesMixin
from amdockvs.qsar.build import BuildMixin
from amdockvs.qsar.jobs import (
    DescriptorJobParams,
    calculate_molecule_alerts_job,
    calculate_molecule_descriptors_job,
)
from amdockvs.qsar.model_analysis import ModelAnalysisMixin
from amdockvs.qsar.modeling import FittedModel, classification_metrics, load_model, regression_metrics
from amdockvs.qsar.predict import PredictMixin
from amdockvs.qsar.results import ResultsMixin
from amdockvs.qsar.tasks import DESCRIPTOR_COLUMNS
from amdockvs.molecules.scopes import MoleculeScope, as_molecule_scope, is_molecule_scope, scope_payload
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef
from amdockvs.workflows.rules import apply_workflow_filters

QSAR_WORKFLOW = "qsar"


@dataclass
class QSARAPI(BuildMixin, PredictMixin, ModelAnalysisMixin, ResultsMixin, ActivitiesMixin):
    runtime: Any

    @staticmethod
    def _model_id(model: QSARModelRef | int) -> int:
        return int(model.id if hasattr(model, "id") else model)

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

    def _design_matrix(
        self, session, ligands, *, feature_kind: str, features, fp_radius: int, fp_nbits: int
    ) -> tuple[list[int], list[list[float]], tuple[str, ...], int]:
        """(used_ids, x_rows, feature_names, n_missing). 'rdkit2d' reads/computes the full RDKit
        block (per-descriptor gaps become NaN for the pipeline's imputer); 'ecfp4' unpacks each
        ligand's stored Morgan fingerprint."""
        if feature_kind == "rdkit2d":
            block = self._rdkit2d_matrix(session, ligands)
            names = tuple(features)
            vectors = {mid: [float(values.get(n, np.nan)) for n in names] for mid, values in block.items() if values}
        else:
            fp_map = self._fingerprint_matrix(
                session, [int(r.id or 0) for r in ligands], radius=fp_radius, nbits=fp_nbits
            )
            names = tuple(f"bit_{i}" for i in range(int(fp_nbits)))
            vectors = {mid: [float(v) for v in vector] for mid, vector in fp_map.items()}
        ids = [int(r.id or 0) for r in ligands if int(r.id or 0) in vectors]
        return ids, [vectors[i] for i in ids], names, len(ligands) - len(ids)

    @staticmethod
    def _features_for(fitted: FittedModel) -> tuple[str, ...]:
        """The feature names the design matrix needs for a fitted model; ecfp4 uses none."""
        return () if fitted.feature_kind == "ecfp4" else tuple(fitted.feature_names)

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
            {"molecule_id": int(r.id), **{name: getattr(r, name, None) for name in DESCRIPTOR_COLUMNS}}
            for r in rows
            if r.id is not None
        ]

    def _load_fitted(self, session, model: QSARModelRef | int) -> tuple[QSARModelRecord, FittedModel]:
        record = session.get(QSARModelRecord, self._model_id(model))
        if record is None:
            raise ValueError(f"Unknown QSAR model id: {self._model_id(model)}")
        fitted = load_model(self._project_root() / record.model_path)
        return record, fitted

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



__all__ = ["QSARAPI"]
