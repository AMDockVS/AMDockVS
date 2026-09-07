from __future__ import annotations

from amdockvs.models import ComplexRecord
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.core.vocab import ComplexPurpose
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
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
        viewer = getattr(main_window, "viewer", None)
        if viewer is not None and viewer.show_complex(pair):
            main_window.aux.show_catalog_selection_details("complex", pair)


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
