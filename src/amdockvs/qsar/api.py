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
    MoleculeRecord,
    MoleculeRepresentation,
    QSARModelRecord,
)
from amdockvs.qsar.activities import ActivitiesMixin
from amdockvs.qsar.build import BuildMixin
from amdockvs.qsar.features import design_matrix, fingerprint_matrix
from amdockvs.qsar.jobs import (
    DescriptorJobParams,
    calculate_molecule_alerts_job,
    calculate_molecule_descriptors_job,
)
from amdockvs.qsar.model_analysis import ModelAnalysisMixin
from amdockvs.qsar.modeling import FittedModel, load_model
from amdockvs.qsar.predict import PredictMixin
from amdockvs.qsar.results import ResultsMixin
from amdockvs.qsar.tasks import DESCRIPTOR_COLUMNS
from amdockvs.qsar.training import score
from amdockvs.molecules.scopes import MoleculeScope, as_molecule_scope, is_molecule_scope, scope_payload
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.project.sets import MoleculeSetRef, QSARModelRef
from amdockvs.workflows.rules import apply_workflow_filters

QSAR_WORKFLOW = "qsar"
# Activities only enter through the As Reference mark, so reference ligands are QSAR data.
QSAR_USAGE_CLASSES = ("general", "reference")


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
        scope = self.runtime.molecules.select(
            source=molecule_set, role="ligand", workflow=QSAR_WORKFLOW, usage_class=QSAR_USAGE_CLASSES
        ).scope
        assert isinstance(scope, MoleculeScope)
        return scope

    def _ligands(self, molecule_set) -> list[MoleculeRecord]:
        return list(self.runtime.molecules.stream(self._qsar_scope(molecule_set)))

    def _design_matrix(
        self, session, ligands, *, feature_kind: str, features, fp_radius: int, fp_nbits: int
    ) -> tuple[list[int], list[list[float]], tuple[str, ...], int]:
        """(used_ids, x_rows, feature_names, n_missing) from the stored features: the rdkit2d
        block or the Morgan fingerprint. Only reads; the descriptor job computes them."""
        ids = [int(r.id or 0) for r in ligands]
        used, rows, names = design_matrix(session, ids, kind=feature_kind, names=features,
                                          fp_radius=fp_radius, fp_nbits=fp_nbits)
        return used, rows, names, len(ids) - len(used)

    @staticmethod
    def _features_for(fitted: FittedModel) -> tuple[str, ...]:
        """The feature names the design matrix needs for a fitted model; ecfp4 uses none."""
        return () if fitted.feature_kind == "ecfp4" else tuple(fitted.feature_names)

    _score = staticmethod(score)

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
        rdkit2d: bool = False,
        molecule_ids: list[int] | None = None,
    ) -> str:
        """Submit the descriptor job. only_missing=True skips ligands that already have
        descriptors computed (MoleculeRecord.mw populated). With compute_fingerprints=True it
        also stores a Morgan fingerprint per ligand, and only_missing then means "lacks that
        fingerprint" so FPs can be added to already-described ligands. rdkit2d=True also stores
        the full RDKit 2D block the models train on (only_missing: lacks that block). With
        alerts=True it is the alerts job instead: PAINS and identity keys, which a library import
        leaves out. molecule_ids narrows the scope to those molecules."""
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
            rdkit2d=bool(rdkit2d),
            molecule_ids=None if molecule_ids is None else [int(i) for i in molecule_ids],
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

    @staticmethod
    def _fingerprint_matrix(session, ligand_ids, *, radius: int, nbits: int) -> dict[int, np.ndarray]:
        return fingerprint_matrix(session, ligand_ids, radius=radius, nbits=nbits)

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
