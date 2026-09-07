"""The `htpvs` library, as far as the project is concerned: an inventory of shards.

In `vs` the Ligands table *is* the library. In `htpvs` the library never enters the project,
so what there is to look at is one row per shard file — how many records it holds and how far
the pipeline got with it. It is the same `BoundTableWidget` over another model; the only
reason it is a separate view is that a shard is not a molecule and no scope can pretend
otherwise (see `molecules/store.py`).

ponytail: read-only. Shards are written by the import and by the chemistry pipeline; there is
nothing a user does to one that isn't "re-run the job" or "delete the project". The submissions
panel the plan pairs with this table (§6) waits for there to be dispatches to show.
"""

from __future__ import annotations

from amdockvs.models import ScreeningShard
from amdockvs.ui.catalog.common import BoundTableWidget
from amdockvs.core.vocab import ShardState
from ms_components.ms_table import (
    AlignHint,
    ColumnDef,
    ColumnKind,
    SortSpec,
    TableConfig,
    TableLoadMode,
    choices_from_class,
)

SHARDS_VIEW_ID = "workspace.shards"


def _shard_table_config() -> TableConfig:
    return TableConfig(
        model_class=ScreeningShard,
        columns=[
            ColumnDef("id", label="ID", width=60, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("shard_index", label="Shard", width=80, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("n_records", label="Records", width=90, sortable=True, align=AlignHint.RIGHT,
                      kind=ColumnKind.INTEGER),
            ColumnDef("state", label="State", width=100, sortable=True, filterable=True,
                      kind=ColumnKind.CHOICE, choices=choices_from_class(ShardState)),
            ColumnDef("source", label="Source", width=360, sortable=True, filterable=True),
            ColumnDef("input_format", label="Format", width=90, sortable=True, filterable=True),
            ColumnDef("error", label="Error", width=240, sortable=False),
            ColumnDef("path", label="Path", width=320, sortable=True, visible=False),
        ],
        default_sort=[SortSpec("id", descending=False)],
        page_size=50,
        page_size_options=[25, 50, 100, 250],
        load_mode=TableLoadMode.INFINITE,
        show_row_numbers=False,
        show_vertical_header=True,
        allow_row_resize=True,
        multi_select=True,
        embedded_controls=True,
        empty_message="No shards in this project — import a library as shards to start a campaign",
    )


class ShardsWidget(BoundTableWidget):
    """Behaves like Ligands/Receptors minus PyMOL: the details panel gets the shard's metadata."""

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


__all__ = ["SHARDS_VIEW_ID", "ShardsWidget", "register_shards_workspace"]
