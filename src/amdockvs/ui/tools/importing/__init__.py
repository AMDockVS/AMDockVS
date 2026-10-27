"""Importing molecules: the ligand and receptor import dialogs and the forms they host."""

from __future__ import annotations

from amdockvs.ui.tools.importing.ligands import LigandImportDialog
from amdockvs.ui.tools.importing.receptors import ReceptorImportDialog


def open_import_view(window, *, kind: str = "ligand"):
    """Both importers are now familiar dialogs."""
    if str(kind).lower().startswith("receptor"):
        dialog = ReceptorImportDialog(runtime=window.runtime, parent=window)
    else:
        dialog = LigandImportDialog(runtime=window.runtime, parent=window)
    window.import_dialog_opened.emit("receptor" if str(kind).lower().startswith("receptor") else "ligand", dialog)
    dialog.exec()
    return dialog


__all__ = ["LigandImportDialog", "ReceptorImportDialog", "open_import_view"]
