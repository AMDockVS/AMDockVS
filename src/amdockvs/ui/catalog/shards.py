"""The `htpvs` library, as far as the project is concerned: an inventory of files.

In `vs` the Ligands table *is* the library. In `htpvs` the library never enters the project,
so what there is to look at is one row per shard file: its shards, the records it
holds (Total) and the ones the exclusions leave (Active). A file being written is listed too, with
the counts it has so far, so an import is watched filling. It is the same `BoundTableWidget` over
another model; the only reason it is a separate view is that a file of shards is not a molecule
and no scope can pretend otherwise (see `molecules/store.py`).

ponytail: read-only. Shards are written by the import and by the chemistry pipeline; there is
nothing a user does to one that isn't "re-run the job" or "delete the project". The submissions
panel the plan pairs with this table (§6) waits for there to be dispatches to show.
"""

from __future__ import annotations

from ms_flow.core.data.catalog import SOURCE
from ms_flow.core.database.project_models import CatalogFile

from amdockvs.ui.catalog.common import BoundTableWidget, batch_columns
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    FilterOperator,
    FilterSpec,
    SortSpec,
    TableConfig,
    TableLoadMode,
)
from amdockvs.ui.view_ids import SHARDS_VIEW_ID


def _shard_table_config() -> TableConfig:
    return TableConfig(
        model_class=CatalogFile,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("path", label="File", width=320, sortable=True),
            *batch_columns(CatalogFile, source_width=230),
            ColumnDef("status", label="Status", width=105, sortable=True),
            ColumnDef("n_shards", label="Shards", width=105, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("n_records", label="Total", width=100, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            # Total minus the exclusions (Filter's cuts): what a step that selects will read.
            ColumnDef("n_active", label="Active", width=100, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("error", label="Error", width=240, sortable=False),
            # Filter by Role for prepared/derived files and result tables (default: the library).
            ColumnDef("role", label="Role", width=130, sortable=True),
            # "" = here; else the queue that wrote the file and keeps it (local reads skip it).
            ColumnDef("location", label="Location", width=110, sortable=True),
            ColumnDef("generation_id", label="Generation", width=90, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER, visible=False),
        ],
        default_filters=[FilterSpec("role", FilterOperator.EQ, SOURCE, label="library")],
        default_sort=[SortSpec("id", descending=False)],
        page_size=50,
        page_size_options=[25, 50, 100, 250],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        embedded_controls=True,
        empty_message="No files in this project - import a library as shards to start a campaign",
    )


class ShardsWidget(BoundTableWidget):
    """Behaves like Ligands/Receptors minus PyMOL: the details panel gets the file's metadata."""

    # Right-click > Select picks the files (their ids) Docking Studio prepares (see PreparationPanel).
    selection_role = "shard"
    selectable = True
    live_refresh_ms = 2000  # an import fills its files: watch them

    def __init__(self, *, runtime, parent=None):
        super().__init__(
            runtime=runtime,
            config=_shard_table_config(),
            empty_text="Open or create a project to inspect its shards.",
            parent=parent,
        )
        if self.table is not None:
            self.table.row_clicked.connect(self._show_shard_details)

    def _load_object_in_pymol(self, obj) -> None:
        self._show_shard_details(obj)

    def _show_shard_details(self, shard) -> None:
        handler = getattr(self.window(), "show_catalog_selection_details", None)
        if callable(handler):
            handler("shard", shard)


def register_shards_workspace(window) -> None:
    window.register_main_view(
        SHARDS_VIEW_ID,
        "Shards",
        lambda: ShardsWidget(runtime=window.runtime, parent=window.central_widget),
    )


__all__ = ["ShardsWidget", "register_shards_workspace"]
