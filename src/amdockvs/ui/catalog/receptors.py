from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from amdockvs.models import MoleculeRecord
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.resources.icons import icon
from amdockvs.core.vocab import FileFormat, MoleculeUsageClass
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    TableConfig,
    TableLoadMode,
    ToolbarAction,
    choices_from_class,
)
from amdockvs.ui.view_ids import COMPLEX_PAIRS_VIEW_ID, RECEPTOR_VIEW_ID


def _receptor_table_config(runtime) -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("name", label="Name", width=220, sortable=True, filterable=True),
            ColumnDef("usage_class", label="Usage", width=110, sortable=True, filterable=True, visible=False,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeUsageClass)),
            ColumnDef("source", label="Source", width=360, sortable=True),
            ColumnDef("input_format", label="Format", width=90, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(FileFormat)),
            ColumnDef("current_payload_id", label="Current Payload", width=130, sortable=True, visible=False),
            ColumnDef("stored_payload_id", label="Stored Payload", width=130, sortable=True, visible=False),
        ],
        default_filters=[
            FilterSpec("is_receptor", FilterOperator.EQ, True, label="is_receptor"),
            FilterSpec("usage_class", FilterOperator.EQ, "general", label="general_only"),
            FilterSpec("excluded", FilterOperator.EQ, False, label="selected_only"),
        ],
        # default_sort=[SortSpec("id", descending=True)],
        page_size=20,
        page_size_options=[10, 20, 50, 100],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        embedded_controls=True,  # Columns/Export · stretch · Reload/Settings
        context_menu_actions={
            "Show Complexes": lambda objs: show_receptor_complexes(QApplication.activeWindow(), objs),
            "Create Receptor Set…": lambda objs: _create_receptor_set(runtime, objs),
        },
        toolbar_left=[
            # ponytail: activeWindow() is the main window at click time (no modal open then).
            ToolbarAction(label="Import…",
                          on_click=lambda objs: import_receptors_from_file(QApplication.activeWindow())),
            ToolbarAction(label="Show Complexes",
                          tooltip="Open the Complexes table with the selected receptors' pairs.",
                          on_click=lambda objs: show_receptor_complexes(QApplication.activeWindow(), objs)),
            # ToolbarAction(label="Create Set…",
            #               on_click=lambda objs: _create_receptor_set(runtime, objs)),
        ],
        empty_message="No receptors loaded in the active project",
        empty_backdrop=icon("protein.svg"),
        empty_action=ToolbarAction(
            label="Import Receptors…",
            on_click=lambda objs: import_receptors_from_file(QApplication.activeWindow()),
        ),
    )


class ReceptorWidget(BoundTableWidget):
    selection_role = "receptor"
    delete_kind = "molecule"
    selectable = True

    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_receptor_table_config(runtime),
            empty_text="Open or create a project to inspect receptors.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._load_receptor_in_pymol)

    def _load_object_in_pymol(self, obj) -> None:
        self._load_receptor_in_pymol(obj)

    def _load_receptor_in_pymol(self, receptor: MoleculeRecord) -> None:
        viewer = getattr(self.window(), "viewer", None)
        if viewer is not None:
            viewer.show_catalog_molecule(
                receptor,
                source="current",
                object_prefix="receptor",
                details_kind="receptor",
                orient=True,
            )


def import_receptors_from_file(window) -> None:
    active_context = getattr(window.runtime, "active_context", None)
    if active_context is None:
        QMessageBox.warning(
            window,
            "Import Receptors",
            "Open or create a project before importing receptors.",
        )
        return
    from amdockvs.ui.tools.importing import open_import_view

    open_import_view(window, kind="receptor")


def show_receptor_complexes(window, receptors) -> None:
    """Complexes is contextual: opening it here is what puts its button on the top bar."""
    ids = [int(r.id) for r in receptors if getattr(r, "id", None)]
    view = window.open_or_focus_view(COMPLEX_PAIRS_VIEW_ID)
    if ids and view is not None:
        label = receptors[0].name if len(ids) == 1 else f"{len(ids)} receptors"
        view.show_for_receptors(ids, label=label or f"receptor {ids[0]}")


def _create_receptor_set(runtime, objects) -> None:
    """Snapshot set from the rows selected in the receptors table (Acciones ▾ / right-click)."""
    ids = [int(getattr(o, "id", 0) or 0) for o in objects if int(getattr(o, "id", 0) or 0) > 0]
    if not ids:
        QMessageBox.information(None, "Create Receptor Set", "Select at least one receptor.")
        return
    default_name = f"receptor_set_{datetime.now():%Y%m%d_%H%M%S}"
    set_name, accepted = QInputDialog.getText(None, "Create Receptor Set", "Set name", text=default_name)
    if not accepted:
        return
    try:
        set_ref = runtime.molecules.create_set(
            ids,
            name=str(set_name or "").strip() or default_name,
            kind="snapshot",
            metadata={"source": "ui.receptors.selection", "count": len(ids)},
        )
    except Exception as exc:
        QMessageBox.critical(None, "Create Receptor Set", str(exc))
        return
    QMessageBox.information(None, "Create Receptor Set", f"Created set #{set_ref.id} with {len(ids)} receptor(s).")


def register_receptors_workspace(window) -> None:
    window.register_main_view(
        RECEPTOR_VIEW_ID,
        "Receptors",
        lambda: ReceptorWidget(
            runtime=window.runtime,
            parent=window.central_widget,
        ),
    )
