"""Delete catalog rows off the GUI thread, then refresh.

Wired to the Delete key and the "Delete…" row action on every catalog table that sets a
`delete_kind` (BoundTableWidget), and to the Results ligand and pose tables. The actual DB work
runs in amdockvs.project.deletion through run_async so a big cascade never freezes the UI."""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QMessageBox

from amdockvs.ui.common.async_query import run_async

# kind -> human description of what also goes away
_KINDS = {
    "molecule": "molecule(s) with their files, docking results, complexes and everything computed from them",
    "complex": "complex pair(s) and their docking results",
    "result": "docking pose(s)",
    "result_group": "ligand result(s) with every pose of their run",
}


def delete_objects(parent, runtime, objects, *, kind: str, done: Callable[[int], None]) -> None:
    """Confirm, delete `objects` (rows of `kind`, or bare ids) off the GUI thread, then call `done(count)`."""
    description = _KINDS[kind]
    ids = sorted({int(getattr(o, "id", o) or 0) for o in objects or ()} - {0})
    if not ids:
        QMessageBox.information(parent, "Delete", "Select at least one row to delete.")
        return
    answer = QMessageBox.question(
        parent,
        "Delete",
        f"Permanently delete {len(ids)} {description}?\n\nThis cannot be undone.",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    delete = {
        "molecule": runtime.molecules.delete,
        "complex": runtime.complexes.delete,
        "result": runtime.docking.delete_results,
        "result_group": lambda ids: runtime.docking.delete_results(ids, with_siblings=True),
    }[kind]
    run_async(
        lambda ids=tuple(ids): delete(ids),
        done,
        on_error=lambda exc: QMessageBox.critical(parent, "Delete", f"Could not delete:\n{exc}"),
        busy=parent,
    )


def delete_selected_objects(widget, *, kind: str, objects=None) -> None:
    """Delete `objects`, or the table's selection, from a BoundTableWidget."""
    table = getattr(widget, "table", None)
    if objects is None:
        if table is None:
            return
        objects = table.get_selected_objects()
    delete_objects(widget, widget.runtime, objects, kind=kind, done=lambda deleted: _after_delete(widget, deleted))


def _after_delete(widget, deleted: int) -> None:
    widget.refresh()
    widget.clear_pymol()
    QMessageBox.information(widget, "Delete", f"Deleted {int(deleted)} row(s).")


__all__ = ["delete_objects", "delete_selected_objects"]
