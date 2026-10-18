"""The dialog a finished run opens when it excluded molecules: one branch per cause, its molecules inside."""
from __future__ import annotations

import re

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ms_components.tool_panel import format_count, hint_label


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
    dialog.setWindowTitle(f"{title}: {format_count(len(rows))} molecules failed")
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
        for molecule_id, name, reason in members:
            QTreeWidgetItem(branch, [name, str(molecule_id), reason])
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
