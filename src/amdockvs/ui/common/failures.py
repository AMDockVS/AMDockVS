"""The dialog a finished run opens when it excluded molecules: one branch per cause, its molecules inside."""
from __future__ import annotations

import re

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ms_components.tool_panel import format_count, hint_label

PER_CAUSE = 50  # molecules listed under a cause; the rest is a count (Filter's "Excluded" has them all)
FETCHED = 10_000  # MoleculeAPI.excluded_since's default limit: that many rows means "or more"


def failure_kind(reason: str) -> str:
    """The cause without its specifics: numbers (atom indices, counts) stop splitting the group."""
    # ponytail: digits are the only per-molecule detail seen in RDKit/step errors; add more
    # normalisation when a message with names or SMILES in it splits a cause.
    return re.sub(r"\b\d+\b", "#", reason.strip().splitlines()[0] if reason.strip() else "unknown")


def show_failures(parent: QWidget, title: str, rows: list[tuple[int, str, str]]) -> QDialog | None:
    """Open (non-modal) the excluded ``(id, name, reason)`` rows grouped by cause; None when there are none."""
    if not rows:
        return None
    dialog = QDialog(parent)
    more = "+" if len(rows) >= FETCHED else ""
    dialog.setWindowTitle(f"{title}: {format_count(len(rows))}{more} molecules failed")
    layout = QVBoxLayout(dialog)
    layout.addWidget(hint_label(
        "These molecules were excluded and later steps skip them. "
        "Filter's \"Excluded\" scope lists them and can bring them back.", dialog,
    ))
    # A thousand failures are a handful of causes: one branch per cause, its molecules inside.
    groups: dict[str, list[tuple[int, str, str]]] = {}
    for row in rows:
        groups.setdefault(failure_kind(row[2]), []).append(row)
    tree = QTreeWidget(dialog)
    tree.setHeaderLabels(["Reason / molecule", "ID", "Error"])
    for kind, members in sorted(groups.items(), key=lambda item: -len(item[1])):
        branch = QTreeWidgetItem(tree, [f"{kind}  ({format_count(len(members))})"])
        for molecule_id, name, reason in members[:PER_CAUSE]:
            QTreeWidgetItem(branch, [name, str(molecule_id), reason])
        if len(members) > PER_CAUSE:  # a mass failure stays a few lines, not a 10k-row list
            QTreeWidgetItem(branch, [f"… and {format_count(len(members) - PER_CAUSE)} more"])
    if len(groups) == 1:
        tree.expandAll()
    tree.resizeColumnToContents(0)
    layout.addWidget(tree)
    buttons = QDialogButtonBox(QDialogButtonBox.Close, dialog)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    dialog.resize(720, 420)
    dialog.show()  # not modal: the user may want the tables behind it
    return dialog
