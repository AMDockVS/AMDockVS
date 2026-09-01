"""Shared QSAR-activity UI helpers: a ligand picker and the CSV-load dialog.

Reused by the activity editor (the Ligand Activity view) and QSAR Models so the two paths —
manual entry and bulk CSV — share one visualization/normalization story.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

UNIT_CHOICES = ["", "nM", "uM", "mM", "M", "pM"]
TRANSFORM_CHOICES = ["(none)", "pIC50", "pKi", "pEC50"]
LIGAND_PICK_CAP = 500  # a manual picker is for tens of ligands; the rest come from a file


def pick_ligands(parent, runtime) -> list[tuple[int, str]]:
    """Modal multi-select over the project's ligands. Returns [(id, name)] (empty if cancelled)."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Add ligands")
    dlg.resize(420, 460)
    layout = QVBoxLayout(dlg)
    table = QTableWidget(0, 3, dlg)
    table.setHorizontalHeaderLabels(["id", "name", "has activity"])
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    table.horizontalHeader().setStretchLastSection(True)
    layout.addWidget(table)

    # ponytail: cap the list — a VS project has 100k+ ligands and a scrollable dialog of all of
    # them is neither loadable nor usable. Say so instead of silently showing a slice.
    ligands = []
    for mol in runtime.molecules.stream(runtime.molecules.select(role="ligand")):
        ligands.append(mol)
        if len(ligands) >= LIGAND_PICK_CAP:
            break
    table.setRowCount(len(ligands))
    for r, mol in enumerate(ligands):
        table.setItem(r, 0, QTableWidgetItem(str(mol.id)))
        table.setItem(r, 1, QTableWidgetItem(str(mol.name or "")))
        table.setItem(r, 2, QTableWidgetItem("yes" if getattr(mol, "has_activity", False) else ""))
    if len(ligands) >= LIGAND_PICK_CAP:
        caption = QLabel(f"Showing the first {LIGAND_PICK_CAP} ligands. Narrow the project's "
                         "ligand set (or import activities from a file) to reach the rest.", dlg)
        caption.setWordWrap(True)
        layout.addWidget(caption)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    if dlg.exec() != QDialog.Accepted:
        return []
    rows = {idx.row() for idx in table.selectedIndexes()}
    return [(int(table.item(r, 0).text()), table.item(r, 1).text()) for r in sorted(rows)]


def load_activities_dialog(parent, *, current_endpoint: str = "") -> dict[str, Any] | None:
    """Prompt for a CSV/TSV + single-endpoint mapping. Returns the kwargs for
    runtime.qsar.load_activities (plus 'file'), or None. Does NOT run the load — the caller runs
    it off the GUI thread so a big table never freezes the UI."""
    path, _ = QFileDialog.getOpenFileName(parent, "Activity table", "", "Tables (*.csv *.tsv *.txt)")
    if not path:
        return None
    dlg = QDialog(parent)
    dlg.setWindowTitle("Load activities")
    form = QFormLayout(dlg)
    endpoint = QLineEdit(current_endpoint or "pIC50", dlg)
    value_col = QLineEdit("value", dlg)
    match = QComboBox(dlg)
    match.addItems(["auto", "inchikey", "smiles", "name"])
    match.setToolTip("How to match CSV rows to ligands. 'auto' prefers InChIKey > SMILES > name.")
    unit = QComboBox(dlg)
    unit.setEditable(True)
    unit.addItems(UNIT_CHOICES)
    transform = QComboBox(dlg)
    transform.addItems(TRANSFORM_CHOICES)
    transform.setToolTip("Convert a concentration value+unit to pX = -log10(M) — the modeling endpoint.")
    form.addRow("Endpoint", endpoint)
    form.addRow("Value column", value_col)
    form.addRow("Match by", match)
    form.addRow("Unit (for transform)", unit)
    form.addRow("Transform", transform)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    if dlg.exec() != QDialog.Accepted:
        return None
    chosen_transform = transform.currentText()
    chosen_transform = None if chosen_transform == "(none)" else chosen_transform
    return {
        "file": path,
        "endpoint": endpoint.text().strip() or "pIC50",
        "value_key": value_col.text().strip() or "value",
        "match_by": match.currentText(),
        "unit": unit.currentText().strip(),
        "transform": chosen_transform,
    }


_COLUMN_ROLES = ["Ignore", "Activity", "Name (match)", "SMILES (match)"]
_NAME_ALIASES = {"name", "id", "title", "compound", "molecule", "mol_id", "molecule_id"}


def _sniff_columns(path: str) -> tuple[list[str], dict[str, str]]:
    """Read the header + a few rows and guess a role per column: SMILES/Name for the id columns,
    Activity for numeric columns, Ignore otherwise. The user confirms/overrides in the dialog."""
    file_path = Path(path)
    delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
    with file_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        columns = list(reader.fieldnames or [])
        sample = [row for _, row in zip(range(40), reader)]
    roles: dict[str, str] = {}
    for col in columns:
        lower = col.strip().lower()
        if "smiles" in lower:
            roles[col] = "SMILES (match)"
        elif lower in _NAME_ALIASES:
            roles[col] = "Name (match)"
        else:
            vals = [str(r.get(col) or "").strip() for r in sample]
            nonempty = [v for v in vals if v]
            numeric = bool(nonempty) and all(_is_float(v) for v in nonempty)
            roles[col] = "Activity" if numeric else "Ignore"
    return columns, roles


def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def map_activity_columns_dialog(parent, path: str = "") -> dict[str, Any] | None:
    """Column-mapping for a wide CSV (e.g. Tox21): auto-detect a role per column, let the user
    fix it (columns often aren't named intuitively), and return the kwargs for
    runtime.qsar.load_activity_matrix. Every column tagged 'Activity' becomes an endpoint; the
    'Name'/'SMILES' tag picks how rows match the already-imported ligands."""
    if not path:
        path, _ = QFileDialog.getOpenFileName(parent, "Activity matrix", "", "Tables (*.csv *.tsv *.txt)")
    if not path:
        return None
    try:
        columns, roles = _sniff_columns(path)
    except Exception as exc:
        QMessageBox.warning(parent, "QSAR", f"Could not read {path}: {exc}")
        return None
    if not columns:
        QMessageBox.warning(parent, "QSAR", "That file has no header row.")
        return None

    dlg = QDialog(parent)
    dlg.setWindowTitle("Map activity columns")
    outer = QVBoxLayout(dlg)
    outer.addWidget(QLabel("Tag each column. 'Activity' columns become endpoints; pick one "
                           "'Name' or 'SMILES' column to match rows to imported ligands.", dlg))
    table = QTableWidget(len(columns), 2, dlg)
    table.setHorizontalHeaderLabels(["Column", "Role"])
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    combos: dict[str, QComboBox] = {}
    for r, col in enumerate(columns):
        name_item = QTableWidgetItem(col)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 0, name_item)
        combo = QComboBox(table)
        combo.addItems(_COLUMN_ROLES)
        combo.setCurrentText(roles.get(col, "Ignore"))
        combos[col] = combo
        table.setCellWidget(r, 1, combo)
    outer.addWidget(table)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    outer.addWidget(buttons)
    dlg.resize(420, 460)
    if dlg.exec() != QDialog.Accepted:
        return None

    picked = {role: [c for c, cb in combos.items() if cb.currentText() == role] for role in _COLUMN_ROLES}
    activity_cols = picked["Activity"]
    if not activity_cols:
        QMessageBox.warning(parent, "QSAR", "Tag at least one column as 'Activity'.")
        return None
    if picked["SMILES (match)"]:
        match_by, key_column = "smiles", picked["SMILES (match)"][0]
    elif picked["Name (match)"]:
        match_by, key_column = "name", picked["Name (match)"][0]
    else:
        match_by, key_column = "auto", None
    return {"file": path, "value_columns": activity_cols, "match_by": match_by, "key_column": key_column}


__all__ = ["TRANSFORM_CHOICES", "UNIT_CHOICES", "load_activities_dialog",
           "map_activity_columns_dialog", "pick_ligands"]
