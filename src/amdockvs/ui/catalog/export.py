"""Export rows of the catalog and results tables: pick a file, write it off the GUI thread.

The writing is `amdockvs.io.export`; this only asks where to and reports the outcome."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from amdockvs.core.constants import RESOURCE_EXPORTS
from amdockvs.core.payloads import molecule_payload_id, payload_store
from amdockvs.io.export import export_poses_sdf, export_structures
from amdockvs.ui.common.async_query import run_async


def _export(parent, runtime, title: str, name: str, pattern: str, work) -> None:
    """Ask for the file, then run `work(store, path) -> count` off the GUI thread."""
    start = Path(runtime.get_project_resource_path(RESOURCE_EXPORTS)) / name
    path, _ = QFileDialog.getSaveFileName(parent, title, str(start), pattern)
    if not path:
        return
    store = payload_store(runtime)
    run_async(
        lambda: work(store, path),
        lambda count: QMessageBox.information(parent, title, f"Exported {int(count)} structure(s) to\n{path}"),
        on_error=lambda exc: QMessageBox.critical(parent, title, f"Could not export:\n{exc}"),
        busy=parent,
    )


def export_poses(parent, runtime, objects) -> None:
    """The poses of the selected result rows as one SDF, scores as data items."""
    ids = [int(o.id) for o in objects or () if getattr(o, "id", None)]
    if not ids:
        QMessageBox.information(parent, "Export poses", "Select at least one pose to export.")
        return

    def work(store, path):
        hits = (runtime.docking.hit(result_id=result_id) for result_id in ids)
        return export_poses_sdf(store, (hit for hit in hits if hit), path)

    _export(parent, runtime, "Export poses", "poses.sdf", "SDF (*.sdf)", work)


def export_pose_files(parent, runtime, objects) -> None:
    """The poses of the selected result rows as a ZIP of SDF files, one per pose."""
    ids = [int(o.id) for o in objects or () if getattr(o, "id", None)]
    if not ids:
        QMessageBox.information(parent, "Export poses", "Select at least one pose to export.")
        return

    def work(store, path):
        hits = (runtime.docking.hit(result_id=result_id) for result_id in ids)
        return export_structures(
            store, [(f"{hit.ligand_name}_{hit.receptor_name}_{hit.result_id}", hit.pose_payload_id)
                    for hit in hits if hit and hit.pose_payload_id], path)

    _export(parent, runtime, "Export poses", "poses.zip", "ZIP (*.zip)", work)


def export_molecules(parent, runtime, objects) -> None:
    """The selected molecules' structures (ligands, receptors) as a ZIP of files in their format."""
    items = [(o.name, molecule_payload_id(o)) for o in objects or () if molecule_payload_id(o)]
    if not items:
        QMessageBox.information(parent, "Export structures", "Select at least one molecule to export.")
        return
    _export(parent, runtime, "Export structures", "structures.zip", "ZIP (*.zip)",
            lambda store, path: export_structures(store, items, path))


__all__ = ["export_molecules", "export_pose_files", "export_poses"]
