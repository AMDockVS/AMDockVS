"""Activities: one value per ligand and endpoint, typed in the editor or loaded from a CSV.

They are written at import time, long before any model is trained, so they live apart from
the modeling code. `QSARAPI` inherits these methods; `runtime.qsar.load_activities(...)` and
the rest are unchanged.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Sequence

from sqlmodel import select

from amdockvs.core.normalize import PathLike
from amdockvs.models import ActivityRecord, MoleculeRecord

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
    from amdockvs.chemistry.descriptors import canonical_smiles, inchikey

    return canonical_smiles(mol) or None, inchikey(mol) or None


def _structural_keys_from_row(project_db, row) -> tuple[str | None, str | None]:
    """(canonical_smiles, inchikey) from a ligand's stored structure, for molecules imported
    before the molecule table carried them."""
    from amdockvs.chemistry.structures import molecule_mol

    return _structural_keys_from_mol(molecule_mol(project_db, row, remove_hs=True))


class ActivitiesMixin:
    """The activity methods of `QSARAPI`; they need only `self.runtime`."""

    runtime: Any

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
        with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
            return sorted({
                str(r) for r in session.exec(select(ActivityRecord.activity_type)).all() if str(r or "").strip()
            })

    def _structural_ligand_index(self, session) -> tuple[dict[str, int], dict[str, int]]:
        """(inchikey->id, canonical_smiles->id) over all ligands, from the molecule table; a
        molecule imported before it had those columns is read from its structure."""
        inchikey_to_id: dict[str, int] = {}
        smiles_to_id: dict[str, int] = {}
        for mol in session.exec(select(MoleculeRecord).where(MoleculeRecord.is_ligand.is_(True))):
            canon, ikey = mol.canonical_smiles, mol.inchikey
            if not (canon or ikey):
                canon, ikey = _structural_keys_from_row(self.runtime.workspace.project_db, mol)
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
        with self.runtime.workspace.project_db.get_session() as session:
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
            with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
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
        with self.runtime.workspace.project_db.get_session() as session:
            rows = session.exec(
                select(ActivityRecord.activity_type, ActivityRecord.kind).distinct()
            ).all()
        return {str(ep): str(k or "continuous") for ep, k in rows if str(ep or "").strip()}
