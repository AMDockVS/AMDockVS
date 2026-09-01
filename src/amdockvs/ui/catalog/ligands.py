from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from amdockvs.molecule_paths import preferred_molecule_path
from amdockvs.models import MoleculeRecord
from amdockvs.ui.resources.icons import icon
from amdockvs.ui.catalog.common import (
    BoundTableWidget,
    PREVIEW_2D_COLUMN_FIELD,
    molecule_2d_preview_paint_for_runtime,
    molecule_2d_preview_tooltip,
)
from amdockvs.ui.tools.pymol_ribbon import (
    apply_ligand_atom_coloring,
    set_pymol_scene_context,
)
from amdockvs.vocab import FileFormat, MoleculeUsageClass
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
    ToolbarAction,
    choices_from_class,
)


LIGANDS_VIEW_ID = "workspace.ligands"

def _ligand_table_config(*, runtime) -> TableConfig:
    return TableConfig(
        model_class=MoleculeRecord,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef(
                PREVIEW_2D_COLUMN_FIELD,
                label="2D Image",
                width=96,
                min_width=96,
                max_width=240,
                sortable=False,
                filterable=False,
                searchable=False,
                resizable=True,
                align=AlignHint.CENTER,
                paint_factory=molecule_2d_preview_paint_for_runtime(runtime),
                tooltip=molecule_2d_preview_tooltip,
                cell_height=84,
            ),
            ColumnDef("name", label="Name", width=220, sortable=True, filterable=True),
            ColumnDef("usage_class", label="Usage", width=110, sortable=True, filterable=True, visible=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeUsageClass)),
            ColumnDef("source", label="Source", width=360, sortable=True),
            ColumnDef("input_format", label="Format", width=90, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(FileFormat)),
            ColumnDef("current_path", label="Current Path", width=300, sortable=True, visible=False),
            ColumnDef("stored_path", label="Stored Path", width=300, sortable=True, visible=False),
        ],
        default_filters=[
            FilterSpec("is_ligand", FilterOperator.EQ, True, label="is_ligand"),
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
        context_menu_actions={"Create Ligand Set…": lambda objs: _create_ligand_set(runtime, objs)},
        toolbar_left=[
            # ponytail: activeWindow() is the main window at click time (no modal open then).
            ToolbarAction(label="Import…",
                          on_click=lambda objs: import_ligands_from_file(QApplication.activeWindow())),
            # ToolbarAction(label="Create Set…",
            #               on_click=lambda objs: _create_ligand_set(runtime, objs)),
        ],
        empty_message="No ligands loaded in the active project",
        empty_action=ToolbarAction(
            label="Import Ligands…", icon=icon("file-plus.svg"),
            on_click=lambda objs: import_ligands_from_file(QApplication.activeWindow()),
        ),
    )


class LigandWidget(BoundTableWidget):
    delete_kind = "molecule"

    def __init__(self, *, runtime, config: TableConfig | None = None, parent=None):
        # `config` lets a step embed the same widget (PyMOL click, delete) with a leaner
        # picker table — see `_docking_ligand_table_config`.
        super().__init__(
            runtime=runtime,
            config=config or _ligand_table_config(runtime=runtime),
            empty_text="Open or create a project to inspect ligands.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._load_ligand_in_pymol)

    def _load_object_in_pymol(self, obj) -> None:
        self._load_ligand_in_pymol(obj)

    def _load_ligand_in_pymol(self, ligand: MoleculeRecord) -> None:
        ligand_path = preferred_molecule_path(ligand) or Path()
        main_window = self.window()
        detail_handler = getattr(main_window, "show_catalog_selection_details", None)
        if callable(detail_handler):
            detail_handler("ligand", ligand)
        if not ligand_path.exists():
            return
        dock = getattr(main_window, "pymol_dock", None)
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        object_name = f"ligand_{getattr(ligand, 'id', 'selected')}"
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(ligand_path), object_name)
            try:
                cmd.show("sticks", object_name)
                apply_ligand_atom_coloring(cmd, object_name)
            except Exception:
                pass
            cmd.zoom(object_name, 3)
            cmd.orient(object_name)
            set_pymol_scene_context(
                dock,
                "ligand",
                target=object_name,
                selections={"ligand": object_name},
                default_preset="amdockvs.ligand",
            )
        except Exception:
            return


def import_ligands_from_file(window) -> None:
    active_context = getattr(window.runtime, "active_context", None)
    if active_context is None:
        QMessageBox.warning(
            window,
            "Import Ligands",
            "Open or create a project before importing ligands.",
        )
        return

    from amdockvs.ui.tools.import_workspace import open_import_view

    open_import_view(window, kind="ligand")


def _create_ligand_set(runtime, objects) -> None:
    """Snapshot set from the rows selected in the ligands table (Acciones ▾ / right-click)."""
    ids = [int(getattr(o, "id", 0) or 0) for o in objects if int(getattr(o, "id", 0) or 0) > 0]
    if not ids:
        QMessageBox.information(None, "Create Ligand Set", "Select at least one ligand.")
        return
    default_name = f"ligand_set_{datetime.now():%Y%m%d_%H%M%S}"
    set_name, accepted = QInputDialog.getText(None, "Create Ligand Set", "Set name", text=default_name)
    if not accepted:
        return
    try:
        set_ref = runtime.molecules.create_set(
            ids,
            name=str(set_name or "").strip() or default_name,
            kind="snapshot",
            metadata={"source": "ui.ligands.selection", "count": len(ids)},
        )
    except Exception as exc:
        QMessageBox.critical(None, "Create Ligand Set", str(exc))
        return
    QMessageBox.information(None, "Create Ligand Set", f"Created set #{set_ref.id} with {len(ids)} ligand(s).")


def register_ligands_workspace(window) -> None:
    window.register_main_view(
        LIGANDS_VIEW_ID,
        "Ligands",
        lambda: LigandWidget(
            runtime=window.runtime,
            parent=window.central_widget,
        ),
    )

