"""Every entry of the main window's view table must name a view that a registrar creates."""
from importlib import import_module

from amdockvs.ui.registry import (
    CATALOG_TABLES, CONTEXTUAL_VIEWS, EPHEMERAL_VIEW_IDS, PREVIEW_VIEW_IDS, REGISTRARS, STANDING_DATA_VIEWS,
    TOOL_VIEW_IDS, TOOLS,
)


def test_registrars_resolve():
    for target in REGISTRARS:
        module_name, _, attr = target.partition(":")
        assert callable(getattr(import_module(module_name), attr)), target


def test_view_tables_are_consistent():
    assert len(TOOL_VIEW_IDS) == len(TOOLS), "duplicate tool view id"
    assert EPHEMERAL_VIEW_IDS <= {v for _, v, _ in CONTEXTUAL_VIEWS}, "ephemeral = contextual"
    listed = {v for _, v, _ in (*CATALOG_TABLES, *CONTEXTUAL_VIEWS)}
    listed |= {v for group in STANDING_DATA_VIEWS for _, v, _ in group}
    assert not (listed & TOOL_VIEW_IDS), "a tool cannot also be a toolbar data view"
    assert PREVIEW_VIEW_IDS <= listed, "a preview view must be reachable from a toolbar"
