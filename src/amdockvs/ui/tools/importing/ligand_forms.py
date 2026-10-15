"""The Filters, Prepare and Activity tabs of the ligand importer; each adds its part to the import policy."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


def _checkbox(text: str, *, checked: bool = False) -> QCheckBox:
    widget = QCheckBox(text)
    widget.setChecked(bool(checked))
    return widget


def _optional_spinbox(*, minimum: int, maximum: int, step: int = 1, default: int = 0) -> QSpinBox:
    widget = QSpinBox()
    widget.setRange(0, maximum)
    widget.setSingleStep(step)
    widget.setSpecialValueText("No limit")
    widget.setValue(default)
    widget.setMinimum(minimum)
    return widget


def _smarts_list(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def finalize_import_prefilter_policy(policy: dict[str, Any]) -> dict[str, Any] | None:
    """Return the policy only if it actually filters or carries an activity mapping."""
    from amdockvs.io.payloads import ImportPrefilterPolicy

    # Activity isn't "active" filtering but must still reach the materializer.
    activity_property = str(policy.get("activity_property") or "").strip()
    activity_columns = tuple(policy.get("activity_columns") or ())
    validated = ImportPrefilterPolicy.model_validate(policy)
    keep = validated.is_active() or validated.prep_active() or activity_property or activity_columns
    return policy if keep else None


class ImportFilterCriteriaForm(QWidget):
    """Cheap drug-likeness / property / substructure filters (streaming scope)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        self.exclude_pains = _checkbox("Exclude PAINS matches", checked=False)
        self.require_ro5 = _checkbox("Require Rule of 5 compliance", checked=False)
        self.max_ro5_violations = _optional_spinbox(minimum=0, maximum=4)
        self.max_rotatable_bonds = _optional_spinbox(minimum=0, maximum=1024, default=32)
        self.max_heavy_atoms = _optional_spinbox(minimum=0, maximum=50000)
        self.mw_min = _optional_spinbox(minimum=0, maximum=5000)
        self.mw_max = _optional_spinbox(minimum=0, maximum=5000)
        mw_row = QHBoxLayout()
        mw_row.addWidget(self.mw_min)
        mw_row.addWidget(QLabel("–"), alignment=Qt.AlignmentFlag.AlignCenter)
        mw_row.addWidget(self.mw_max)
        self.include_smarts = QLineEdit(self)
        self.include_smarts.setPlaceholderText("SMARTS, comma-separated — keep matches")
        self.exclude_smarts = QLineEdit(self)
        self.exclude_smarts.setPlaceholderText("SMARTS, comma-separated — drop matches")
        layout.addRow(self.exclude_pains)
        layout.addRow(self.require_ro5)
        layout.addRow("Max Ro5 violations", self.max_ro5_violations)
        layout.addRow("Max Rotatable Bonds", self.max_rotatable_bonds)
        layout.addRow("Max Heavy Atoms", self.max_heavy_atoms)
        layout.addRow("MW range (0 = off)", mw_row)
        layout.addRow("Include SMARTS", self.include_smarts)
        layout.addRow("Exclude SMARTS", self.exclude_smarts)

    def contribute(self, policy: dict[str, Any]) -> None:
        policy.update(
            {
                "exclude_pains": self.exclude_pains.isChecked(),
                "require_ro5": self.require_ro5.isChecked(),
                "max_ro5_violations": int(self.max_ro5_violations.value()) if int(self.max_ro5_violations.value()) > 0 else None,
                "max_rotatable_bonds": int(self.max_rotatable_bonds.value()) or None,
                "max_heavy_atoms": int(self.max_heavy_atoms.value()) or None,
                "include_smarts": _smarts_list(self.include_smarts.text()),
                "exclude_smarts": _smarts_list(self.exclude_smarts.text()),
            }
        )
        mw_lo = float(self.mw_min.value()) or None
        mw_hi = float(self.mw_max.value()) or None
        if mw_lo is not None or mw_hi is not None:
            policy["property_ranges"] = {"mw": (mw_lo, mw_hi)}


class ImportPrepareForm(QWidget):
    """Opt-in structure prep applied to the kept ligand fragment at import (each only if missing)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        self.add_hs = _checkbox("Add hydrogens (if missing)", checked=False)
        self.gen_3d = _checkbox("Generate 3D coordinates (if missing)", checked=False)
        self.canonical_tautomer = _checkbox("Standardize to canonical tautomer", checked=False)
        self.gen_3d.setToolTip("Embeds an ETKDG conformer and adds hydrogens — slow over huge libraries.")
        self.canonical_tautomer.setToolTip(
            "Picks ONE canonical tautomer per molecule. Enumerating tautomers into separate "
            "molecules is a distinct step (planned for Molecule Tools), not this checkbox."
        )
        self.split_fragments = _checkbox("Split fragments into separate molecules", checked=False)
        self.split_fragments.setToolTip(
            "Import keeps the largest organic fragment of each record. With this on, every other "
            "distinct organic fragment enters as its own molecule (duplicates and ions skipped)."
        )
        layout.addRow(self.add_hs)
        layout.addRow(self.gen_3d)
        layout.addRow(self.canonical_tautomer)
        layout.addRow(self.split_fragments)

    def contribute(self, policy: dict[str, Any]) -> None:
        policy.update(
            {
                "add_hs": self.add_hs.isChecked(),
                "gen_3d": self.gen_3d.isChecked(),
                "canonical_tautomer": self.canonical_tautomer.isChecked(),
                "split_fragments": self.split_fragments.isChecked(),
            }
        )


_ACTIVITY_STRUCTURAL = {"smiles", "canonical_smiles", "smiles_canonical", "inchikey", "inchi_key",
                        "inchi key", "name", "id", "title", "compound", "molecule", "mol_id", "molecule_id"}


def _sniff_activity_columns(path: str) -> dict[str, str]:
    """Read the header + a few rows of a CSV/SMILES table and return {numeric column -> kind}
    ('categorical' if every sampled value is a 0/1 label, else 'continuous'). Non-numeric and
    structural (smiles/name/id) columns are omitted — they can't be activity endpoints."""
    file_path = Path(path)
    if file_path.suffix.lower() not in {".csv", ".tsv", ".txt", ".smi", ".smiles"}:
        return {}
    delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        columns = list(reader.fieldnames or [])
        sample = [row for _, row in zip(range(40), reader)]
    kinds: dict[str, str] = {}
    for col in columns:
        if col.strip().lower() in _ACTIVITY_STRUCTURAL:
            continue
        values = [str(r.get(col) or "").strip() for r in sample]
        nonempty = [v for v in values if v]
        if not nonempty:
            continue
        parsed = []
        for v in nonempty:
            try:
                parsed.append(float(v))
            except ValueError:
                parsed = None
                break
        if parsed is None:
            continue  # not a numeric column
        kinds[col] = "categorical" if all(p in (0.0, 1.0) for p in parsed) else "continuous"
    return kinds


class ImportActivityForm(QWidget):
    """Activity ingestion at import: one 'Activity from tag' (with optional pIC50 transform) AND a
    multi-column chip picker that auto-detects every numeric column (e.g. Tox21's 12 assays) so a
    wide CSV becomes molecules + many activity endpoints in a single import."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        single = QFormLayout()
        self.activity_property = QLineEdit(self)
        self.activity_property.setPlaceholderText("SDF tag or CSV column, e.g. IC50 (blank = off)")
        self.activity_endpoint = QLineEdit(self)
        self.activity_endpoint.setPlaceholderText("endpoint (defaults to tag)")
        self.activity_unit = QComboBox(self)
        self.activity_unit.setEditable(True)
        self.activity_unit.addItems(["", "nM", "uM", "mM", "M", "pM"])
        self.activity_transform = QComboBox(self)
        self.activity_transform.addItems(["(none)", "pIC50", "pKi", "pEC50"])
        activity_row = QHBoxLayout()
        for w in (self.activity_property, self.activity_endpoint, self.activity_unit, self.activity_transform):
            activity_row.addWidget(w)
        single.addRow("Activity from file tag/column", activity_row)
        layout.addLayout(single)

        self.columns_box = QGroupBox("Activity columns (auto-detected from the CSV)", self)
        self.columns_box.setToolTip("Tick each numeric column to load as its own endpoint. "
                                    "0/1 columns are stored as categorical, others as continuous.")
        box_layout = QVBoxLayout(self.columns_box)
        self._chips_host = QWidget(self.columns_box)
        self._chips_layout = QVBoxLayout(self._chips_host)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self.columns_box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self._chips_host)
        box_layout.addWidget(scroll)
        layout.addWidget(self.columns_box)
        self._chips: dict[str, QCheckBox] = {}
        self._detected_kinds: dict[str, str] = {}
        self.columns_box.setVisible(False)

    def set_source_file(self, path: str | None) -> None:
        """Called by the import dialog when the file list changes: sniff the CSV's numeric columns
        and offer them as unticked chips. Hidden for SDF / no file.

        Unticked on purpose: a numeric column is not an assay, and pre-ticking turned every
        tabular import into an activity import."""
        for chip in self._chips.values():
            chip.setParent(None)
        self._chips.clear()
        self._detected_kinds = _sniff_activity_columns(path) if path else {}
        if not self._detected_kinds:
            self.columns_box.setVisible(False)
            return
        for column, kind in self._detected_kinds.items():
            chip = QCheckBox(f"{column}  ({kind})", self._chips_host)
            self._chips_layout.addWidget(chip)
            self._chips[column] = chip
        self.columns_box.setVisible(True)

    def contribute(self, policy: dict[str, Any]) -> None:
        activity_property = self.activity_property.text().strip()
        if activity_property:
            transform = self.activity_transform.currentText()
            policy["activity_property"] = activity_property
            policy["activity_endpoint"] = self.activity_endpoint.text().strip()
            policy["activity_unit"] = self.activity_unit.currentText().strip()
            policy["activity_transform"] = "" if transform == "(none)" else transform
        picked = [col for col, chip in self._chips.items() if chip.isChecked()]
        if picked:
            policy["activity_columns"] = tuple(picked)
            policy["activity_kinds"] = {col: self._detected_kinds[col] for col in picked}


__all__ = ["ImportActivityForm", "ImportFilterCriteriaForm", "ImportPrepareForm", "finalize_import_prefilter_policy"]
