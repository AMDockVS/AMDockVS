from __future__ import annotations

from amdockvs.models import BindingSite
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.vocab import BindingSiteSource
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    SortSpec,
    TableConfig,
    TableLoadMode,
    choices_from_class,
)


BINDING_SITES_VIEW_ID = "workspace.binding_sites"


def _binding_site_table_config() -> TableConfig:
    return TableConfig(
        model_class=BindingSite,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("molecule_id", label="Receptor", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("name", label="Name", width=180, sortable=True, filterable=True),
            ColumnDef("source", label="Source", width=100, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(BindingSiteSource)),
            ColumnDef("source_ref", label="Source Ref", width=180, sortable=True),
            ColumnDef("center_x", label="Center X", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("center_y", label="Center Y", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("center_z", label="Center Z", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("size_x", label="Size X", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("size_y", label="Size Y", width=90, sortable=True, align=AlignHint.RIGHT),
            ColumnDef("size_z", label="Size Z", width=90, sortable=True, align=AlignHint.RIGHT),
        ],
        default_sort=[SortSpec("molecule_id", descending=False), SortSpec("id", descending=False)],
        page_size=50,
        page_size_options=[25, 50, 100, 250],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        empty_message="No binding sites loaded in the active project",
    )


class BindingSiteWidget(BoundTableWidget):
    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_binding_site_table_config(),
            empty_text="Open or create a project to inspect binding sites.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._show_binding_site)

    def _show_binding_site(self, site: BindingSite) -> None:
        receptor = self.runtime.molecules.get(int(site.molecule_id or 0))
        if receptor is None:
            return
        main_window = self.window()
        detail_handler = getattr(main_window, "show_catalog_selection_details", None)
        if callable(detail_handler):
            detail_handler("molecule", receptor)
        site_handler = getattr(main_window, "_show_binding_site_from_details", None)
        if callable(site_handler):
            site_handler(receptor, site)


def open_binding_sites_view(window) -> None:
    window.open_or_focus_view(BINDING_SITES_VIEW_ID)


def register_binding_sites_workspace(window) -> None:
    window.register_main_view(
        BINDING_SITES_VIEW_ID,
        "Binding Sites",
        lambda: BindingSiteWidget(runtime=window.runtime, parent=window.central_widget),
    )



__all__ = [
    "BINDING_SITES_VIEW_ID",
    "BindingSiteWidget",
    "open_binding_sites_view",
    "register_binding_sites_workspace",
]
