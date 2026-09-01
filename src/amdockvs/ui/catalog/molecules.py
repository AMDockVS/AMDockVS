from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from amdockvs.molecule_paths import current_molecule_path, stored_molecule_path
from amdockvs.models import MoleculeRecord
from amdockvs.ui.tools.pymol_ribbon import (
    apply_ligand_atom_coloring,
    apply_receptor_atom_coloring,
    apply_scene_atom_coloring,
    set_pymol_scene_context,
)
from amdockvs.ui.catalog.common import (
    BoundTableWidget,
    PREVIEW_2D_COLUMN_FIELD,
    display_project_relative_path,
    molecule_2d_preview_paint_for_runtime,
    molecule_2d_preview_tooltip,
)
from amdockvs.vocab import FileFormat, MoleculeType, MoleculeUsageClass
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
    choices_from_class,
)

MOLECULES_VIEW_ID = "workspace.molecules"


def _molecule_table_config(*, runtime) -> TableConfig:
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
            ColumnDef("molecule_type", label="Type", width=120, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeType)),
            ColumnDef("usage_class", label="Usage", width=110, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(MoleculeUsageClass)),
            ColumnDef("n_atoms", label="Atoms", width=105, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("input_format", label="Format", width=115, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(FileFormat)),
            ColumnDef("source", label="Source", width=320, sortable=True),
            ColumnDef("current_model_index", label="Active Model", width=130, sortable=True, visible=False),
            ColumnDef(
                "current_path",
                label="Current Path",
                width=300,
                sortable=True,
                visible=False,
                formatter=display_project_relative_path,
            ),
            ColumnDef(
                "stored_path",
                label="Stored Path",
                width=300,
                sortable=True,
                visible=False,
                formatter=display_project_relative_path,
            ),
        ],
        # default_sort=[SortSpec("id", descending=True)],
        page_size=25,
        page_size_options=[10, 25, 50, 100, 250],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        embedded_controls=True,  # ☰ columns + ⟳ refresh (native to the component)
        empty_message="No molecules loaded in the active project",
    )


class MoleculeWidget(BoundTableWidget):
    delete_kind = "molecule"

    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_molecule_table_config(runtime=runtime),
            empty_text="Open or create a project to inspect molecules.",
            parent=parent,
        )
        self._structure_sources: dict[str, str] = {}
        if self.table is not None:
            self.table.row_clicked.connect(self._load_molecule_in_pymol)

    def push_scope(self, key: str, *, structure_source: str | None = None, **scope) -> None:
        if structure_source is not None:
            normalized = (
                "current" if str(structure_source).strip().lower() == "current" else "original"
            )
            if self._structure_sources.get(key) != normalized:
                self._structure_sources[key] = normalized
                self._reload_selected_structure()
        super().push_scope(key, **scope)

    def pop_scope(self, key: str) -> None:
        if self._structure_sources.pop(key, None) is not None:
            self._reload_selected_structure()
        super().pop_scope(key)

    def _reload_selected_structure(self) -> None:
        selected = self.table.get_selected_object() if self.table is not None else None
        if selected is not None:
            self._load_molecule_in_pymol(selected)

    def _load_object_in_pymol(self, obj) -> None:
        self._load_molecule_in_pymol(obj)

    def _load_molecule_in_pymol(self, molecule: MoleculeRecord) -> None:
        source = next(reversed(self._structure_sources.values()), "original")
        molecule_path = (
            current_molecule_path(molecule) if source == "current" else stored_molecule_path(molecule)
        ) or Path()
        main_window = self.window()
        detail_handler = getattr(main_window, "show_catalog_selection_details", None)
        if callable(detail_handler):
            detail_handler("molecule", molecule)
        if not molecule_path.exists():
            return
        dock = getattr(main_window, "pymol_dock", None)
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        object_name = f"molecule_{getattr(molecule, 'id', 'selected')}"
        try:
            dock.show()
            cmd.delete("all")
            cmd.load(str(molecule_path), object_name)
            if bool(getattr(molecule, "is_ligand", False)):
                context_kind = "ligand"
                context_role = "ligand"
                default_preset = "amdockvs.ligand"
                try:
                    cmd.show("sticks", object_name)
                    apply_ligand_atom_coloring(cmd, object_name)
                except Exception:
                    pass
            elif bool(getattr(molecule, "is_receptor", False)):
                context_kind = "receptor"
                context_role = "receptor"
                default_preset = "amdockvs.receptor"
                apply_receptor_atom_coloring(cmd, object_name)
            else:
                context_kind = "generic"
                context_role = "molecule"
                default_preset = ""
                apply_scene_atom_coloring(main_window)
            cmd.zoom(object_name, 3)
            set_pymol_scene_context(
                dock,
                context_kind,
                target=object_name,
                selections={context_role: object_name},
                default_preset=default_preset,
            )
        except Exception:
            return


def _require_project(window, *, title: str) -> bool:
    active_context = getattr(window.runtime, "active_context", None)
    if active_context is not None:
        return True
    QMessageBox.warning(
        window,
        title,
        "Open or create a project before using this action.",
    )
    return False


def import_ligands_from_file(window) -> None:
    if not _require_project(window, title="Import Ligands"):
        return
    # Single ligand importer everywhere: the familiar drag-drop dialog.
    from amdockvs.ui.tools.import_workspace import open_import_view

    open_import_view(window, kind="ligand")


def import_receptors_from_file(window) -> None:
    from amdockvs.ui.catalog.receptors import import_receptors_from_file as import_receptors_from_receptor_catalog

    import_receptors_from_receptor_catalog(window)


def register_molecules_workspace(window) -> None:
    window.register_main_view(
        MOLECULES_VIEW_ID,
        "Molecules",
        lambda: MoleculeWidget(runtime=window.runtime, parent=window.central_widget),
    )



__all__ = [
    "MOLECULES_VIEW_ID",
    "MoleculeWidget",
    "register_molecules_workspace",
]
