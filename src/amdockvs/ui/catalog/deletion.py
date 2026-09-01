"""Delete the selected catalog rows off the GUI thread, then refresh.

Wired to a 'Delete' ribbon action and the Delete key on every catalog table (via
BoundTableWidget.delete_selected). The actual DB work runs in amdockvs.deletion through
run_async so a big cascade never freezes the UI."""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from amdockvs.ui.async_query import run_async

# kind -> human description of what also goes away
_KINDS = {
    "molecule": "molecule(s) and their docking results, complexes and descriptors",
    "complex": "complex pair(s) and their docking results",
    "result": "docking result(s)",
}


def delete_selected_objects(widget, *, kind: str) -> None:
    description = _KINDS[kind]
    table = getattr(widget, "table", None)
    if table is None:
        return
    objs = table.get_selected_objects()
    ids = [int(getattr(o, "id", 0) or 0) for o in objs if int(getattr(o, "id", 0) or 0) > 0]
    if not ids:
        QMessageBox.information(widget, "Delete", "Select at least one row to delete.")
        return
    answer = QMessageBox.question(
        widget,
        "Delete",
        f"Permanently delete {len(ids)} {description}?\n\nThis cannot be undone.",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    delete = {
        "molecule": widget.runtime.molecules.delete,
        "complex": widget.runtime.complexes.delete,
        "result": widget.runtime.docking.delete_results,
    }[kind]
    run_async(
        lambda ids=tuple(ids): delete(ids),
        lambda deleted: _after_delete(widget, deleted),
        on_error=lambda exc: QMessageBox.critical(widget, "Delete", f"Could not delete:\n{exc}"),
        busy=widget,
    )


def _after_delete(widget, deleted: int) -> None:
    widget.refresh()
    widget.clear_pymol()
    QMessageBox.information(widget, "Delete", f"Deleted {int(deleted)} row(s).")


__all__ = ["delete_selected_objects"]
