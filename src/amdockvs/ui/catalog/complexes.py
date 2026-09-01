from __future__ import annotations

from pathlib import Path

from amdockvs.models import ComplexRecord, MoleculeRecord
from amdockvs.molecule_paths import current_molecule_path, get_default_project_root, stored_molecule_path
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.ui.tools.pymol_ribbon import (
    apply_receptor_atom_coloring,
    apply_receptor_ligand_atom_coloring,
    set_pymol_scene_context,
)
from amdockvs.vocab import ComplexPurpose
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    SortSpec,
    TableConfig,
    TableLoadMode,
    choices_from_class,
)


COMPLEX_PAIRS_VIEW_ID = "workspace.complex_pairs"


def _complex_table_config() -> TableConfig:
    return TableConfig(
        model_class=ComplexRecord,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("name", label="Name", width=220, sortable=True, filterable=True),
            ColumnDef("purpose", label="Purpose", width=110, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(ComplexPurpose)),
            ColumnDef("receptor_molecule_id", label="Receptor", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("ligand_molecule_id", label="Ligand", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("binding_site_id", label="BS", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("activity_id", label="Activity", width=80, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("reference_receptor_path", label="Reference Path", width=240, sortable=True, visible=False),
            ColumnDef("created_at", label="Created", width=180, sortable=True),
        ],
        # default_sort=None,
        page_size=25,
        page_size_options=[10, 25, 50, 100],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        empty_message="No receptor-ligand pairs loaded in the active project",
    )


class ComplexPairsWidget(BoundTableWidget):
    delete_kind = "complex"

    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_complex_table_config(),
            empty_text="Open or create a project to inspect receptor-ligand pairs.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._load_pair_in_pymol)

    def _load_object_in_pymol(self, obj) -> None:
        self._load_pair_in_pymol(obj)

    def _load_pair_in_pymol(self, pair: ComplexRecord) -> None:
        main_window = self.window()
        dock = getattr(main_window, "pymol_dock", None)
        if dock is None:
            return
        cmd = getattr(dock, "cmd", None)
        if cmd is None:
            return
        reference_path = self._reference_receptor_path(pair)
        receptor = self._molecule_by_id(int(getattr(pair, "receptor_molecule_id", 0) or 0))
        ligand = self._molecule_by_id(int(getattr(pair, "ligand_molecule_id", 0) or 0))
        if reference_path is None or not reference_path.exists():
            reference_path = current_molecule_path(receptor) if receptor is not None else None
        ligand_path = stored_molecule_path(ligand) if ligand is not None else None
        if reference_path is None or not reference_path.exists():
            return
        try:
            dock.show()
            cmd.delete("all")
            receptor_obj = f"complex_receptor_{int(pair.id or 0)}"
            ligand_obj = f"complex_ligand_{int(pair.id or 0)}"
            cmd.load(str(reference_path), receptor_obj)
            if ligand_path is not None and ligand_path.exists():
                cmd.load(str(ligand_path), ligand_obj)
                try:
                    cmd.show("sticks", ligand_obj)
                    apply_receptor_ligand_atom_coloring(
                        cmd,
                        receptor_selection=receptor_obj,
                        ligand_selections=[ligand_obj],
                    )
                    cmd.orient(ligand_obj)
                except Exception:
                    pass
            else:
                apply_receptor_atom_coloring(cmd, receptor_obj)
            cmd.zoom("all", 3)
            selections = {"receptor": receptor_obj}
            if ligand_path is not None and ligand_path.exists():
                selections["ligand"] = ligand_obj
            set_pymol_scene_context(
                dock,
                "complex",
                target="all",
                selections=selections,
                default_preset="amdockvs.complex",
            )
        except Exception:
            return
        detail_handler = getattr(main_window, "show_catalog_selection_details", None)
        if callable(detail_handler):
            detail_handler("complex", pair)

    def _molecule_by_id(self, molecule_id: int) -> MoleculeRecord | None:
        if molecule_id <= 0:
            return None
        return self.runtime.molecules.get(molecule_id)

    @staticmethod
    def _reference_receptor_path(pair: ComplexRecord) -> Path | None:
        raw = str(getattr(pair, "reference_receptor_path", "") or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        project_root = get_default_project_root()
        return (project_root / path).resolve() if project_root is not None else path.resolve()


def open_complex_pairs_view(window) -> None:
    window.open_or_focus_view(COMPLEX_PAIRS_VIEW_ID)


def register_complex_pairs_workspace(window) -> None:
    window.register_main_view(
        COMPLEX_PAIRS_VIEW_ID,
        "Complexes",
        lambda: ComplexPairsWidget(runtime=window.runtime, parent=window.central_widget),
    )



__all__ = [
    "COMPLEX_PAIRS_VIEW_ID",
    "ComplexPairsWidget",
    "open_complex_pairs_view",
    "register_complex_pairs_workspace",
]
