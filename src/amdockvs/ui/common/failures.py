"""The dialog a finished run opens when it excluded molecules: one branch per cause, its molecules inside."""
from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ms_components.tool_panel import format_count, hint_label

PER_CAUSE = 50  # molecules listed under a cause; the rest is a count (Re-include takes the whole cause)
FETCHED = 10_000  # MoleculeAPI.excluded_since's default limit: that many rows means "or more"


def failure_kind(reason: str) -> str:
    """The cause without its specifics: numbers (atom indices, counts) stop splitting the group."""
    # ponytail: digits are the only per-molecule detail seen in RDKit/step errors; add more
    # normalisation when a message with names or SMILES in it splits a cause.
    return re.sub(r"\b\d+\b", "#", reason.strip().splitlines()[0] if reason.strip() else "unknown")


def recover_with(runtime, after: Callable[[], None] | None = None) -> Callable[[list[int]], int]:
    """A ``recover`` for :func:`show_failures`: re-include through the molecules API, then refresh.

    Exclusion is not owned by the step that set it — a receptor the preparation excluded comes
    back the same way a filtered ligand does, which is why this knows nothing of either.
    """
    def recover(molecule_ids: list[int]) -> int:
        count = int(runtime.molecules.set_excluded_state(molecule_ids, excluded=False))
        if after is not None:
            after()
        return count

    return recover


def _item_ids(items) -> set[int]:
    """The molecules a tree selection stands for: a cause means all of its members, listed or not."""
    return {int(value) for item in items for value in (item.data(0, Qt.UserRole) or ())}


def _add_recover_button(
    dialog: QDialog,
    buttons: QDialogButtonBox,
    tree: QTreeWidget,
    rows: list[tuple[int, str, str]],
    recover: Callable[[list[int]], int],
    hint: QLabel,
) -> None:
    """The way back out of an exclusion, offered where the exclusion is reported.

    It acts on the selection, or on everything listed when there is none — a cause at a time is
    what a user wants after fixing one of them. It re-includes; it does not retry: the point is
    to get the molecules back into scope so the step can be run again with other settings.
    """
    button = buttons.addButton("Re-include", QDialogButtonBox.ActionRole)
    recovered: set[int] = set()

    def chosen() -> list[int]:
        picked = _item_ids(tree.selectedItems()) or {int(row[0]) for row in rows}
        return sorted(picked - recovered)

    def sync() -> None:
        count = len(chosen())
        selected = bool(_item_ids(tree.selectedItems()))
        button.setText(f"Re-include {format_count(count)} selected" if selected
                       else f"Re-include all {format_count(count)}")
        button.setEnabled(bool(count))

    def run() -> None:
        ids = chosen()
        button.setEnabled(False)
        try:
            count = int(recover(ids))
        except Exception as exc:  # noqa: BLE001 - the user asked for this; say why it did not happen
            QMessageBox.critical(dialog, "Re-include molecules", str(exc))
            sync()
            return
        recovered.update(ids)
        # The list stays as it is: it is the record of what this run excluded, not a live view.
        hint.setText(f"Brought back {format_count(count)} molecule(s) — they are active again. "
                     "Change what made them fail and run the step once more.")
        sync()

    button.clicked.connect(run)
    tree.itemSelectionChanged.connect(sync)
    sync()


def show_failures(
    parent: QWidget,
    title: str,
    rows: list[tuple[int, str, str]],
    recover: Callable[[list[int]], int] | None = None,
) -> QDialog | None:
    """Open (non-modal) the excluded ``(id, name, reason)`` rows grouped by cause; None when there are none.

    ``recover`` re-includes the molecule ids it is given and returns how many came back; it is
    what the dialog's Re-include button calls. Without it the dialog only reports.
    """
    if not rows:
        return None
    dialog = QDialog(parent)
    more = "+" if len(rows) >= FETCHED else ""
    dialog.setWindowTitle(f"{title}: {format_count(len(rows))}{more} molecules failed")
    layout = QVBoxLayout(dialog)
    hint = hint_label(
        "These molecules were excluded and later steps skip them."
        + (" Pick a cause or a molecule and use Re-include to bring them back."
           if recover is not None else ""), dialog,
    )
    layout.addWidget(hint)
    # A thousand failures are a handful of causes: one branch per cause, its molecules inside.
    groups: dict[str, list[tuple[int, str, str]]] = {}
    for row in rows:
        groups.setdefault(failure_kind(row[2]), []).append(row)
    tree = QTreeWidget(dialog)
    tree.setHeaderLabels(["Reason / molecule", "ID", "Error"])
    tree.setSelectionMode(QTreeWidget.ExtendedSelection)
    for kind, members in sorted(groups.items(), key=lambda item: -len(item[1])):
        branch = QTreeWidgetItem(tree, [f"{kind}  ({format_count(len(members))})"])
        branch.setData(0, Qt.UserRole, [int(row[0]) for row in members])  # the whole cause
        for molecule_id, name, reason in members[:PER_CAUSE]:
            leaf = QTreeWidgetItem(branch, [name, str(molecule_id), reason])
            leaf.setData(0, Qt.UserRole, [int(molecule_id)])
        if len(members) > PER_CAUSE:  # a mass failure stays a few lines, not a 10k-row list
            QTreeWidgetItem(branch, [f"… and {format_count(len(members) - PER_CAUSE)} more"])
    if len(groups) == 1:
        tree.expandAll()
    tree.resizeColumnToContents(0)
    layout.addWidget(tree)
    buttons = QDialogButtonBox(QDialogButtonBox.Close, dialog)
    buttons.rejected.connect(dialog.close)
    if recover is not None:
        _add_recover_button(dialog, buttons, tree, rows, recover, hint)
    layout.addWidget(buttons)
    dialog.resize(720, 420)
    dialog.show()  # not modal: the user may want the tables behind it
    return dialog
