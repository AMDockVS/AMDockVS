"""Lean QSAR API — activities, descriptor-based features, simple sklearn model templates.

No dataset/split/role machinery: train takes a molecule set + an activity endpoint and
fits a template estimator; predict scores any molecule set. Features come straight from
the descriptor columns on MoleculeRecord (populated by the descriptor job). Designed to
work standalone in script/notebook mode; the UI just calls these methods.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from sqlmodel import select

from amdockvs.constants import DEFAULT_LOCAL_CPU_EXECUTOR, RESOURCE_QSAR_MODELS
from amdockvs.models import (
    ActivityRecord,
    FingerprintRecord,
    MoleculeRecord,
    MoleculeRepresentation,
    QSARModelRecord,
    QSARPredictionRecord,
)
from amdockvs.models.descriptors import FingerprintType
from amdockvs.models.molecules import ReprType

# Concentration units -> molar factor, for pX = -log10(M) transforms.
_UNIT_TO_MOLAR = {"m": 1.0, "mm": 1e-3, "um": 1e-6, "µm": 1e-6, "nm": 1e-9, "pm": 1e-12, "": 1.0}


def _to_pchem(value: float, unit: str) -> float | None:
    """IC50/Ki/EC50 concentration -> pX = -log10(value in molar). None if unit unknown or value<=0."""
    factor = _UNIT_TO_MOLAR.get(str(unit or "").strip().lower())
    if factor is None or value <= 0:
        return None
    return round(-math.log10(value * factor), 4)


def _structural_keys_from_smiles(smiles: str) -> tuple[str | None, str | None]:
    """(canonical_smiles, inchikey) computed from a SMILES string, or (None, None) if unparseable.
    Used to match a CSV's SMILES column against the ligand index (inchikey preferred — it's
    representation-independent, so it survives SMILES formatting differences)."""
    try:
        from rdkit import Chem
    except ImportError:
        return None, None
    mol = Chem.MolFromSmiles(str(smiles or "").strip())
    return _structural_keys_from_mol(mol)


def _structural_keys_from_mol(mol) -> tuple[str | None, str | None]:
    if mol is None:
        return None, None
    try:
        from rdkit import Chem

        return Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)
    except Exception:
        return None, None


def _structural_keys_from_file(path: Path) -> tuple[str | None, str | None]:
    """(canonical_smiles, inchikey) from a ligand's stored structure file. Lets activity loading
    match by structure even though the importer doesn't persist representations yet."""
    try:
        from rdkit import Chem
    except ImportError:
        return None, None
    suffix = path.suffix.lower()
    try:
        if suffix in {".sdf", ".mol"}:
            mol = next(iter(Chem.SDMolSupplier(str(path), sanitize=True, removeHs=True)), None)
        elif suffix == ".mol2":
            mol = Chem.MolFromMol2File(str(path), sanitize=True, removeHs=True)
        elif suffix in {".pdb", ".ent"}:
            mol = Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=True)
        else:
            return None, None
    except Exception:
        return None, None
    return _structural_keys_from_mol(mol)
from amdockvs.qsar.jobs import DescriptorJobParams, calculate_molecule_descriptors_job
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
from amdockvs.api_common import MoleculeScope, PathLike, scope_payload
from amdockvs.molecules.api import ensure_molecule_set_ref
from amdockvs.scopes import MoleculeSetRef, QSARModelRef
from amdockvs.workflows import apply_workflow_filters

QSAR_WORKFLOW = "qsar"


@dataclass
class QSARAPI:
    runtime: Any

    # --- scope / feature helpers ---------------------------------------------
    def _qsar_scope(self, molecule_set: MoleculeSetRef | MoleculeScope | int | None) -> MoleculeScope:
        if isinstance(molecule_set, MoleculeScope):
            return MoleculeScope(
                filters=apply_workflow_filters(molecule_set.filters, workflow=QSAR_WORKFLOW, role="ligand"),
                source_set_id=molecule_set.source_set_id,
                order=tuple(molecule_set.order or ("id",)),
                limit=molecule_set.limit,
            )
        if molecule_set is None:
            return self.runtime.molecules.select(role="ligand", workflow=QSAR_WORKFLOW)
        return self.runtime.molecules.select(source=molecule_set, role="ligand", workflow=QSAR_WORKFLOW)

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
        from amdockvs.molecule_paths import preferred_molecule_path

        try:
            from rdkit import Chem
            from rdkit.Chem.Scaffolds import MurckoScaffold
        except ImportError:
            return {}
        out: dict[int, str] = {}
        for rec in records:
            mid = int(rec.id or 0)
            path = preferred_molecule_path(rec)
            mol = None
            if path is not None and path.exists():
                suffix = path.suffix.lower()
                if suffix in {".sdf", ".mol"}:
                    mol = next(iter(Chem.SDMolSupplier(str(path), sanitize=True, removeHs=True)), None)
                elif suffix == ".mol2":
                    mol = Chem.MolFromMol2File(str(path), sanitize=True, removeHs=True)
                elif suffix in {".pdb", ".ent"}:
                    mol = Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=True)
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

    # --- activities -----------------------------------------------------------
    def set_activity(
        self,
        *,
        ligand_id: int,
        endpoint: str,
        value: float,
        unit: str = "",
        source: str = "",
        description: str = "",
        replace: bool = True,
    ) -> ActivityRecord:
        """Create or edit a ligand's activity for an endpoint (activity_type). With
        replace=True (default) it overwrites existing values for that (ligand, endpoint)."""
        self.runtime._require_active_project()
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("set_activity requires endpoint.")
        with self.runtime.molsuite.project_db.get_session() as session:
            molecule = session.get(MoleculeRecord, int(ligand_id))
            if molecule is None:
                raise ValueError(f"Molecule {ligand_id} does not exist.")
            if replace:
                existing = session.exec(
                    select(ActivityRecord)
                    .where(ActivityRecord.molecule_id == int(ligand_id))
                    .where(ActivityRecord.activity_type == endpoint_text)
                ).all()
                for row in existing:
                    session.delete(row)
            record = ActivityRecord(
                molecule_id=int(ligand_id),
                value=float(value),
                unit=str(unit or ""),
                activity_type=endpoint_text,
                description=str(description or ""),
                source=str(source or ""),
            )
            session.add(record)
            molecule.has_activity = True
            session.add(molecule)
            session.commit()
            session.refresh(record)
            return record

    def delete_activity(self, *, ligand_id: int, endpoint: str | None = None) -> int:
        """Delete a ligand's activities (all, or only for one endpoint). Returns count removed."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            statement = select(ActivityRecord).where(ActivityRecord.molecule_id == int(ligand_id))
            if endpoint is not None:
                statement = statement.where(ActivityRecord.activity_type == str(endpoint))
            rows = session.exec(statement).all()
            for row in rows:
                session.delete(row)
            remaining = session.exec(
                select(ActivityRecord).where(ActivityRecord.molecule_id == int(ligand_id))
            ).first()
            molecule = session.get(MoleculeRecord, int(ligand_id))
            if molecule is not None:
                molecule.has_activity = remaining is not None
                session.add(molecule)
            session.commit()
            return len(rows)

    def list_activities(
        self,
        *,
        endpoint: str | None = None,
        ligand_id: int | None = None,
    ) -> list[ActivityRecord]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            statement = select(ActivityRecord)
            if endpoint is not None:
                statement = statement.where(ActivityRecord.activity_type == str(endpoint))
            if ligand_id is not None:
                statement = statement.where(ActivityRecord.molecule_id == int(ligand_id))
            return list(session.exec(statement.order_by(ActivityRecord.id)))

    def activity_rows(self, *, endpoint: str | None = None) -> list[dict[str, Any]]:
        """Activities joined with the ligand name, for the editor table:
        [{molecule_id, name, value, unit}] ordered by ligand id."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            statement = select(ActivityRecord)
            if endpoint is not None:
                statement = statement.where(ActivityRecord.activity_type == str(endpoint))
            rows = list(session.exec(statement.order_by(ActivityRecord.molecule_id)))
            names = {
                int(m.id): str(m.name or "")
                for m in session.exec(
                    select(MoleculeRecord).where(MoleculeRecord.id.in_([int(r.molecule_id) for r in rows] or [0]))
                )
            }
        return [
            {"molecule_id": int(r.molecule_id), "name": names.get(int(r.molecule_id), ""),
             "value": float(r.value), "unit": str(r.unit or "")}
            for r in rows
        ]

    def list_endpoints(self) -> list[str]:
        """Distinct activity endpoints present in the project."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            return sorted({
                str(r) for r in session.exec(select(ActivityRecord.activity_type)).all() if str(r or "").strip()
            })

    def _structural_ligand_index(self, session) -> tuple[dict[str, int], dict[str, int]]:
        """(inchikey->id, canonical_smiles->id) over all ligands. Uses persisted representations
        when present (fast), otherwise computes keys from each ligand's stored structure file."""
        from amdockvs.molecule_paths import preferred_molecule_path

        inchikey_to_id = {
            str(r.value): int(r.molecule_id)
            for r in session.exec(
                select(MoleculeRepresentation).where(MoleculeRepresentation.repr_type == ReprType.INCHI_KEY)
            )
        }
        smiles_to_id = {
            str(r.value): int(r.molecule_id)
            for r in session.exec(
                select(MoleculeRepresentation).where(MoleculeRepresentation.repr_type == ReprType.SMILES_CANONICAL)
            )
        }
        if inchikey_to_id or smiles_to_id:
            return inchikey_to_id, smiles_to_id
        # fallback: derive identity from the stored structure (importer doesn't persist reprs yet)
        for mol in session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))):
            if mol.id is None:
                continue
            path = preferred_molecule_path(mol)
            if path is None or not path.exists():
                continue
            canon, ikey = _structural_keys_from_file(path)
            if ikey:
                inchikey_to_id.setdefault(ikey, int(mol.id))
            if canon:
                smiles_to_id.setdefault(canon, int(mol.id))
        return inchikey_to_id, smiles_to_id

    @staticmethod
    def _detect_match(fieldnames: Sequence[str], match_by: str, key_column: str | None) -> tuple[str, str]:
        """Resolve (mode, column): mode in {inchikey, smiles, name}, column = the CSV header to read
        the key from. 'auto' prefers structural keys (inchikey > smiles) over name."""
        lower = {str(f).strip().lower(): f for f in fieldnames}
        candidates = [
            ("inchikey", ("inchikey", "inchi_key", "inchi key")),
            ("smiles", ("smiles", "canonical_smiles", "smiles_canonical")),
            ("name", ("name", "id", "title", "compound", "molecule", "mol_id", "molecule_id")),
        ]
        if match_by != "auto":
            mode = "name" if match_by not in {"inchikey", "smiles"} else match_by
            if key_column and key_column in fieldnames:
                return mode, key_column
            for name, aliases in candidates:
                if name == mode:
                    col = next((lower[a] for a in aliases if a in lower), None)
                    if col is not None:
                        return mode, col
            raise ValueError(f"No column for match_by={match_by!r} in header {list(fieldnames)}")
        if key_column and key_column in fieldnames:
            return "name", key_column
        for mode, aliases in candidates:
            col = next((lower[a] for a in aliases if a in lower), None)
            if col is not None:
                return mode, col
        raise ValueError(f"Could not auto-detect a ligand key column in header {list(fieldnames)}")

    def _import_missing_molecules(
        self, file_path: Path, *, match_by: str, key_column: str | None,
        delimiter: str | None, executor_name: str,
    ) -> int:
        """Import (via the real ligand importer) molecules for CSV rows whose ligand isn't in the
        project yet — matched the same way load_activities matches — so no duplicates are created.
        Returns the count requested. # ponytail: blocks on the import job; fine for interactive loads."""
        import os
        import tempfile

        if delimiter is None:
            delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
        smiles_col = next((c for c in fieldnames if "smiles" in c.strip().lower()), None)
        if smiles_col is None:
            raise ValueError("import_missing needs a SMILES column in the CSV.")
        mode, key_col = self._detect_match(fieldnames, match_by, key_column)
        with self.runtime.molsuite.project_db.get_session() as session:
            if mode == "name":
                existing = {
                    str(r.name) for r in session.exec(
                        select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))
                    ) if r.name
                }

                def is_present(row) -> bool:
                    return str(row.get(key_col) or "").strip() in existing
            else:
                inchikey_to_id, smiles_to_id = self._structural_ligand_index(session)

                def is_present(row) -> bool:
                    key = str(row.get(key_col) or "").strip()
                    if mode == "inchikey":
                        return key.upper() in inchikey_to_id
                    canon, ikey = _structural_keys_from_smiles(key)
                    return bool((ikey and ikey in inchikey_to_id) or (canon and canon in smiles_to_id))

        missing: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            smiles = str(row.get(smiles_col) or "").strip()
            name = str(row.get(key_col) or "").strip()
            if not smiles or is_present(row):
                continue
            dedup_key = name or smiles
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            missing.append((smiles, name))
        if not missing:
            return 0
        fd, tmp_path = tempfile.mkstemp(suffix=".smi", prefix="amdock_missing_")
        os.close(fd)
        tmp = Path(tmp_path)
        tmp.write_text("SMILES,Name\n" + "\n".join(f"{s},{n}" for s, n in missing) + "\n")

        def _ligand_count() -> int:
            with self.runtime.molsuite.project_db.get_session() as session:
                return len(session.exec(
                    select(MoleculeRecord.id).where(MoleculeRecord.is_ligand.is_(True))
                ).all())

        before = _ligand_count()
        try:
            jobs = self.runtime.loader.load_ligands([tmp], executor_name=executor_name)
            self.runtime.wait_for_jobs(list(jobs))
        finally:
            tmp.unlink(missing_ok=True)
        # Actual molecules created — the importer drops rows RDKit can't sanitize, so this can be
        # < len(missing); report the truth so the UI message isn't inflated.
        return _ligand_count() - before

    def load_activities(
        self,
        file: PathLike,
        *,
        endpoint: str,
        value_key: str = "value",
        match_by: str = "auto",
        key_column: str | None = None,
        unit: str = "",
        unit_key: str | None = None,
        transform: str | None = None,
        kind: str = "continuous",
        source: str = "",
        replace: bool = True,
        delimiter: str | None = None,
        import_missing: bool = False,
        executor_name: str = "compute",
    ) -> dict[str, Any]:
        """Bulk-load activities from a CSV/TSV.

        match_by: 'auto' (default — prefer InChIKey, then SMILES, then name), 'inchikey', 'smiles'
        or 'name'. Structural keys match the stored representations (InChIKey is robust to SMILES
        formatting). transform (e.g. 'pIC50'/'pKi') converts a concentration value+unit to
        pX = -log10(M) — the modeling endpoint — keeping the raw value in the description.
        replace=True wipes prior activities for the matched ligands+endpoint first (idempotent reload).
        import_missing=True first imports (via the real ligand importer) any CSV row whose molecule
        isn't in the project yet, so a fresh CSV becomes molecules+activities in one call.
        """
        self.runtime._require_active_project()
        file_path = Path(file).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        if import_missing:
            self._import_missing_molecules(
                file_path, match_by=match_by, key_column=key_column,
                delimiter=delimiter, executor_name=executor_name,
            )
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("load_activities requires endpoint.")
        if delimiter is None:
            delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
        loaded = skipped_missing = skipped_invalid = 0
        with self.runtime.molsuite.project_db.get_session() as session:
            with file_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                if reader.fieldnames is None:
                    raise ValueError(f"Activity file has no header: {file_path}")
                mode, key_col = self._detect_match(reader.fieldnames, match_by, key_column)
                name_to_id: dict[str, int] = {}
                inchikey_to_id: dict[str, int] = {}
                smiles_to_id: dict[str, int] = {}
                if mode == "name":
                    name_to_id = {
                        str(r.name): int(r.id)
                        for r in session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True)))
                        if r.id is not None
                    }
                else:
                    inchikey_to_id, smiles_to_id = self._structural_ligand_index(session)

                rows_to_add: list[tuple[int, float, str, str]] = []  # (lid, value, unit, description)
                for raw in reader:
                    key = str(raw.get(key_col) or "").strip()
                    if mode == "name":
                        lid = name_to_id.get(key)
                    elif mode == "inchikey":
                        lid = inchikey_to_id.get(key.upper())
                    else:  # smiles — canonicalize the CSV smiles, prefer InChIKey identity
                        canon, ikey = _structural_keys_from_smiles(key)
                        lid = (inchikey_to_id.get(ikey) if ikey else None) or (smiles_to_id.get(canon) if canon else None)
                    if lid is None:
                        skipped_missing += 1
                        continue
                    try:
                        raw_value = float(str(raw.get(value_key) or "").strip())
                    except ValueError:
                        skipped_invalid += 1
                        continue
                    row_unit = str((raw.get(unit_key) if unit_key else unit) or "").strip()
                    if transform:
                        transformed = _to_pchem(raw_value, row_unit)
                        if transformed is None:
                            skipped_invalid += 1
                            continue
                        rows_to_add.append((lid, transformed, transform, f"raw={raw_value} {row_unit}".strip()))
                    else:
                        rows_to_add.append((lid, raw_value, row_unit, ""))

            # 'auto' -> categorical when every value is a 0/1 label, else continuous.
            effective_kind = str(kind or "continuous")
            if effective_kind == "auto":
                effective_kind = "categorical" if rows_to_add and all(
                    v in (0.0, 1.0) for _, v, *_ in rows_to_add
                ) else "continuous"

            touched = {lid for lid, *_ in rows_to_add}
            if replace and touched:
                for stale in session.exec(
                    select(ActivityRecord)
                    .where(ActivityRecord.activity_type == endpoint_text)
                    .where(ActivityRecord.molecule_id.in_(touched))
                ).all():
                    session.delete(stale)
            for lid, value, value_unit, description in rows_to_add:
                session.add(
                    ActivityRecord(
                        molecule_id=lid,
                        value=value,
                        unit=value_unit,
                        activity_type=endpoint_text,
                        kind=effective_kind,
                        source=str(source or file_path.name),
                        description=description,
                    )
                )
                loaded += 1
            for lid in touched:
                molecule = session.get(MoleculeRecord, lid)
                if molecule is not None:
                    molecule.has_activity = True
                    session.add(molecule)
            session.commit()
        return {
            "file": str(file_path),
            "endpoint": endpoint_text,
            "kind": effective_kind,
            "match_by": mode,
            "key_column": key_col,
            "transform": transform or "",
            "loaded": loaded,
            "skipped_missing_ligand": skipped_missing,
            "skipped_invalid_value": skipped_invalid,
        }

    def load_activity_matrix(
        self,
        file: PathLike,
        *,
        value_columns: Sequence[str] | None = None,
        kind: str = "auto",
        match_by: str = "name",
        key_column: str | None = None,
        source: str = "",
        replace: bool = True,
        delimiter: str | None = None,
        import_missing: bool = False,
        executor_name: str = "compute",
    ) -> dict[str, Any]:
        """Load a wide CSV where several columns are each an activity endpoint (e.g. Tox21's 12
        assays). value_columns=None auto-picks every column except the structural/name keys.
        Each column becomes one endpoint (activity_type=column); empty cells are skipped so a
        sparse assay matrix loads cleanly. kind 'auto' (default) records categorical for 0/1-label
        columns and continuous otherwise, per column; pass 'categorical'/'continuous' to force it.
        import_missing=True first imports molecules for rows not yet in the project (one importer
        pass), then loads every endpoint — turning a raw CSV into molecules+activities in one call."""
        file_path = Path(file).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        if delimiter is None:
            delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
        imported = 0
        if import_missing:
            imported = self._import_missing_molecules(
                file_path, match_by=match_by, key_column=key_column,
                delimiter=delimiter, executor_name=executor_name,
            )
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            header = csv.DictReader(handle, delimiter=delimiter).fieldnames or []
        if not header:
            raise ValueError(f"Activity file has no header: {file_path}")
        structural = {"smiles", "canonical_smiles", "smiles_canonical", "inchikey", "inchi_key",
                      "inchi key", "name", "id", "title", "compound", "molecule", "mol_id", "molecule_id"}
        if value_columns is None:
            value_columns = [c for c in header if c.strip().lower() not in structural and c != key_column]
        results = [
            self.load_activities(
                file_path, endpoint=col, value_key=col, kind=kind, match_by=match_by,
                key_column=key_column, source=source, replace=replace, delimiter=delimiter,
            )
            for col in value_columns
        ]
        return {
            "file": str(file_path),
            "endpoints": list(value_columns),
            "imported_molecules": imported,
            "loaded": sum(r["loaded"] for r in results),
            "per_endpoint": {r["endpoint"]: r["loaded"] for r in results},
            "kinds": {r["endpoint"]: r["kind"] for r in results},
        }

    def endpoint_kinds(self) -> dict[str, str]:
        """{endpoint: 'categorical'|'continuous'} so the model UI can default the task."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = session.exec(
                select(ActivityRecord.activity_type, ActivityRecord.kind).distinct()
            ).all()
        return {str(ep): str(k or "continuous") for ep, k in rows if str(ep or "").strip()}

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
    ) -> str:
        """Submit the descriptor job. only_missing=True skips ligands that already have
        descriptors computed (MoleculeRecord.mw populated). With compute_fingerprints=True it
        also stores a Morgan fingerprint per ligand, and only_missing then means "lacks that
        fingerprint" so FPs can be added to already-described ligands."""
        self.runtime._require_active_project()
        molecule_set_ref = (
            None
            if molecule_set is None or isinstance(molecule_set, MoleculeScope)
            else ensure_molecule_set_ref(self.runtime, molecule_set, name="qsar_descriptor_input")
        )
        molecule_scope = scope_payload(molecule_set) if isinstance(molecule_set, MoleculeScope) else {}
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
        )
        return self.runtime.submit_job(
            calculate_molecule_descriptors_job,
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
        with self.runtime.molsuite.project_db.get_session() as session:
            if not train_ids:  # model recorded no split → use every labeled training ligand as reference
                record = session.get(QSARModelRecord, int(model.id if hasattr(model, "id") else model))
                train_ids = [int(m) for m in ((record.metrics or {}).get("usable_ligands") or [])] if record else []
            fp_map = self._fingerprint_matrix(
                session, train_ids + [int(r.id or 0) for r in ligands], radius=fp_radius, nbits=fp_nbits
            )
            names = {int(r.id or 0): str(r.name or "") for r in ligands}
        train_fps = [fp_map[i] for i in train_ids if i in fp_map]
        if not train_fps:
            raise ValueError("No training fingerprints — compute Morgan fingerprints (ECFP4) first.")
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
        with self.runtime.molsuite.project_db.get_session() as session:
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
        with self.runtime.molsuite.project_db.get_session() as session:
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
        with self.runtime.molsuite.project_db.get_session() as session:
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
            note = f"class {pos} fraction — train {fr(train_idx):.3f} vs test {fr(test_idx):.3f}"
            return {"task": "classification", "categories": [str(c) for c in classes],
                    "train": train, "test": test, "n_train": len(train_idx), "n_test": len(test_idx), "note": note}
        lo, hi = float(y.min()), float(y.max())
        edges = np.linspace(lo, hi, int(bins) + 1) if hi > lo else np.array([lo, lo + 1.0])
        cats = [f"{edges[i]:.2g}" for i in range(len(edges) - 1)]
        train = [int(v) for v in np.histogram(y[train_idx], bins=edges)[0]]
        test = [int(v) for v in np.histogram(y[test_idx], bins=edges)[0]]
        note = f"mean value — train {float(y[train_idx].mean()):.3f} vs test {float(y[test_idx].mean()):.3f}"
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
        whole Bemis-Murcko scaffolds. cv_folds>=2 adds a CV score (Q²/MCC). class_threshold binarizes
        a *continuous* endpoint (active = value >= threshold); leave None for categorical endpoints.
        y_scramble>=1 runs that many label-permutation refits (should score ~0; else signal is spurious)."""
        self.runtime._require_active_project()
        endpoint_text = str(endpoint or "").strip()
        if not endpoint_text:
            raise ValueError("train requires endpoint.")
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
        with self.runtime.molsuite.project_db.get_session() as session:
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
                "All ligands fall in a single class after labeling — a model can't be trained. "
                "For a categorical endpoint don't set a class threshold (the value is already the "
                "label); for a continuous one, pick a threshold that leaves both classes populated."
            )

        x_train, y_train, holdout = x_matrix, y_vector, None
        test_ids: set[int] = set()  # for split-membership reporting (train/test; no separate val set)
        if test_size and 0.0 < float(test_size) < 1.0 and x_matrix.shape[0] >= 4:
            if split == "scaffold":
                from amdockvs.qsar.modeling import grouped_holdout_split

                train_idx, test_idx = grouped_holdout_split(
                    [scaffolds.get(mid, "") for mid in used_ids], test_size=float(test_size), seed=int(seed)
                )
                if test_idx and train_idx:
                    x_train, y_train = x_matrix[train_idx], y_vector[train_idx]
                    holdout = (x_matrix[test_idx], y_vector[test_idx])
                    test_ids = {used_ids[i] for i in test_idx}
            else:
                from sklearn.model_selection import train_test_split

                indices = np.arange(x_matrix.shape[0])
                train_i, test_i = train_test_split(indices, test_size=float(test_size), random_state=int(seed))
                x_train, y_train = x_matrix[train_i], y_vector[train_i]
                holdout = (x_matrix[test_i], y_vector[test_i])
                test_ids = {used_ids[i] for i in test_i}

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
            "corr_threshold": float(corr_threshold),
            "n_train": int(x_train.shape[0]),
            "train": self._score(task, y_train, fitted, x_train),
            "usable_ligands": used_ids,
            "split_assignment": [[mid, "test" if mid in test_ids else "train"] for mid in used_ids],
        }
        if holdout is not None:
            metrics["test"] = self._score(task, holdout[1], fitted, holdout[0])
        if cv_folds and int(cv_folds) >= 2:
            from amdockvs.qsar.modeling import cross_val_score_mean

            metrics["cv_folds"] = int(cv_folds)
            cv_score = cross_val_score_mean(
                x_matrix, y_vector, algorithm=algorithm, task=task, cv=int(cv_folds),
                hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=float(corr_threshold),
            )
            metrics["q2" if task == "regression" else "cv_mcc"] = cv_score
        if y_scramble and int(y_scramble) >= 1:
            from amdockvs.qsar.modeling import y_scramble_score

            metrics["y_scramble"] = y_scramble_score(
                x_matrix, y_vector, algorithm=algorithm, task=task, n_permutations=int(y_scramble),
                hyperparams=hyperparams, feature_kind=feature_kind, corr_threshold=float(corr_threshold), seed=int(seed),
            )
        importance = feature_importance(fitted.estimator, out_names)
        if importance:
            metrics["feature_importance"] = [[n, v] for n, v in importance]

        with self.runtime.molsuite.project_db.get_session() as session:
            model_record = QSARModelRecord(
                name=str(name or "").strip() or f"{algorithm}_{endpoint_text}",
                algorithm=algorithm,
                target=endpoint_text,
                feature_type="descriptors_2d",
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
        with self.runtime.molsuite.project_db.get_session() as session:
            model_record = QSARModelRecord(
                name=str(name or source_path.stem),
                algorithm=fitted.algorithm,
                target=str(endpoint or ""),
                feature_type="descriptors_2d",
                source="external",
                metrics={"task": fitted.task, "features": list(fitted.feature_names)},
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

    def roc_curve_points(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        endpoint: str | None = None,
        subset: str = "all",
    ) -> dict[str, Any]:
        """(fpr, tpr) ROC points + AUC for a binary classification model. subset 'test' (held-out —
        the honest predictivity), 'train' (optimistic; the gap to test shows overfitting) or 'all'.
        Classification-only; needs a probabilistic model and both classes present."""
        self.runtime._require_active_project()
        ligands = self._ligands(molecule_set)
        with self.runtime.molsuite.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            if fitted.task != "classification":
                raise ValueError("ROC curve is only for classification models.")
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
            raise ValueError(f"No labeled, feature-complete ligands in the '{subset}' subset for a ROC curve.")
        x_matrix = np.asarray(x_rows, dtype=float)
        y_raw = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
        y_true = (y_raw >= float(threshold)).astype(int) if threshold is not None else y_raw.astype(int)
        # A stale/0.0 threshold on a categorical 0/1 endpoint collapses every label to one class —
        # fall back to the raw labels (the endpoint is already categorical) so old models still plot.
        if threshold is not None and len(set(y_true.tolist())) != 2:
            y_true = y_raw.astype(int)
        estimator = fitted.estimator
        if not hasattr(estimator, "predict_proba") or len(set(y_true.tolist())) != 2:
            raise ValueError("ROC needs a probabilistic binary model with both classes labeled.")
        from sklearn.metrics import roc_auc_score, roc_curve

        proba = np.asarray(estimator.predict_proba(x_matrix))
        if proba.ndim != 2 or proba.shape[1] < 2:
            raise ValueError("model was trained on a single class — retrain it (no ROC possible).")
        scores = proba[:, 1]
        fpr, tpr, _thr = roc_curve(y_true, scores)
        return {
            "endpoint": endpoint_text,
            "subset": str(subset or "all").lower(),
            "points": [(float(a), float(b)) for a, b in zip(fpr, tpr)],
            "auc": float(roc_auc_score(y_true, scores)),
            "n": int(len(y_true)),
        }

    def _load_fitted(self, session, model: QSARModelRef | int) -> tuple[QSARModelRecord, FittedModel]:
        model_id = int(model.id if hasattr(model, "id") else model)
        record = session.get(QSARModelRecord, model_id)
        if record is None:
            raise ValueError(f"Unknown QSAR model id: {model_id}")
        fitted = load_model(self._project_root() / record.model_path)
        return record, fitted

    def list_models(self) -> list[QSARModelRecord]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
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
        with self.runtime.molsuite.project_db.get_session() as session:
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
                    )
                )
            session.commit()
            return {
                "model_id": model_id,
                "predicted": len(ids),
                "skipped_missing_descriptor": len(ligands) - len(ids),
            }

    def list_predictions(
        self,
        *,
        model: QSARModelRef | int | None = None,
        ligand_ids: Sequence[int] | None = None,
    ) -> list[QSARPredictionRecord]:
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
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
        [{molecule_id, name, value, confidence}] ordered by ligand id."""
        self.runtime._require_active_project()
        model_id = int(model.id if hasattr(model, "id") else model)
        with self.runtime.molsuite.project_db.get_session() as session:
            rows = list(session.exec(
                select(QSARPredictionRecord)
                .where(QSARPredictionRecord.model_id == model_id)
                .order_by(QSARPredictionRecord.molecule_id)
            ))
            names = {
                int(m.id): str(m.name or "")
                for m in session.exec(
                    select(MoleculeRecord).where(MoleculeRecord.id.in_([int(r.molecule_id) for r in rows] or [0]))
                )
            }
        return [
            {"molecule_id": int(r.molecule_id), "name": names.get(int(r.molecule_id), ""),
             "value": float(r.value), "confidence": None if r.confidence is None else float(r.confidence)}
            for r in rows
        ]

    def evaluate(
        self,
        *,
        model: QSARModelRef | int,
        molecule_set: MoleculeSetRef | MoleculeScope | int | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Score a labeled molecule set against a model and return task-appropriate metrics."""
        self.runtime._require_active_project()
        ligands = self._ligands(molecule_set)
        with self.runtime.molsuite.project_db.get_session() as session:
            record, fitted = self._load_fitted(session, model)
            endpoint_text = str(endpoint or record.target or "").strip()
            if not endpoint_text:
                raise ValueError("evaluate needs an endpoint (model has no target).")
            activity_by_ligand = self._latest_activity_by_ligand(session, endpoint_text)
            features = self._features_for(fitted)
            threshold = (record.metrics or {}).get("class_threshold")
            labeled = [r for r in ligands if int(r.id or 0) in activity_by_ligand]
            used_ids, x_rows, _names, _missing = self._design_matrix(
                session, labeled, feature_kind=fitted.feature_kind, features=features,
                fp_radius=int(fitted.fp_radius or 2), fp_nbits=int(fitted.fp_nbits or 2048),
            )
            if not x_rows:
                raise ValueError("No labeled, descriptor-complete ligands to evaluate.")
            x_matrix = np.asarray(x_rows, dtype=float)
            y_raw = np.asarray([activity_by_ligand[mid] for mid in used_ids], dtype=float)
            if fitted.task == "classification":
                y_eval = (y_raw >= float(threshold)).astype(int) if threshold is not None else y_raw.astype(int)
                metrics = self._score("classification", y_eval, fitted, x_matrix)
            else:
                metrics = self._score("regression", y_raw, fitted, x_matrix)
            metrics["model_id"] = int(record.id or 0)
            metrics["endpoint"] = endpoint_text
            return metrics

    def model_subsets(self, *, model: QSARModelRef | int) -> dict[int, str]:
        """{ligand_id: 'train'|'test'} for a trained model's split (empty if not recorded).
        The lean pipeline has no separate validation set — only train/test."""
        self.runtime._require_active_project()
        with self.runtime.molsuite.project_db.get_session() as session:
            record = session.get(QSARModelRecord, int(model.id if hasattr(model, "id") else model))
            if record is None:
                return {}
            return {int(mid): str(sub) for mid, sub in (record.metrics or {}).get("split_assignment", [])}

    def _ligand_mol(self, session, rec):
        """An RDKit mol for a ligand: from its stored structure file, else its canonical SMILES."""
        from rdkit import Chem

        from amdockvs.molecule_paths import preferred_molecule_path

        path = preferred_molecule_path(rec)
        if path is not None and path.exists():
            suffix = path.suffix.lower()
            if suffix in {".sdf", ".mol"}:
                return next(iter(Chem.SDMolSupplier(str(path), sanitize=True, removeHs=True)), None)
            if suffix == ".mol2":
                return Chem.MolFromMol2File(str(path), sanitize=True, removeHs=True)
            if suffix in {".pdb", ".ent"}:
                return Chem.MolFromPDBFile(str(path), sanitize=True, removeHs=True)
        rep = session.exec(
            select(MoleculeRepresentation)
            .where(MoleculeRepresentation.molecule_id == int(rec.id or 0))
            .where(MoleculeRepresentation.repr_type == ReprType.SMILES_CANONICAL)
        ).first()
        return Chem.MolFromSmiles(str(rep.value)) if rep is not None else None

    def atom_contributions(self, *, model: QSARModelRef | int, ligand_id: int) -> dict[str, Any]:
        """Per-atom contribution to an ECFP4 model's prediction (StarDrop's "glowing molecule").
        Returns {molblock, weights[per atom], prediction}. ECFP4 models only."""
        self.runtime._require_active_project()
        from rdkit import Chem, DataStructs
        from rdkit.Chem.Draw import SimilarityMaps

        with self.runtime.molsuite.project_db.get_session() as session:
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

        def pred_fn(fp):
            arr = np.zeros((nbits,), dtype=np.float64)
            DataStructs.ConvertToNumpyArray(fp, arr)
            return float(fitted.predict(arr.reshape(1, -1))[0])

        weights = SimilarityMaps.GetAtomicWeightsForModel(mol, fp_fn, pred_fn)
        return {
            "molblock": Chem.MolToMolBlock(mol),
            "weights": [float(w) for w in weights],
            "prediction": pred_fn(fp_fn(mol)),
        }


__all__ = ["QSARAPI"]
