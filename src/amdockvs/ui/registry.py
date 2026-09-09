"""The one table that says what the main window shows.

Adding a tool used to mean editing four places: the registrar call in `main_window`, the
left-toolbar row, the set that routes it to the tool dock instead of a tab, and the view
id itself. Here it is one `ToolEntry` row — `TOOL_VIEW_IDS` is derived from it, and the
registrar is named by string so this table imports without pulling in every widget.

`REGISTRARS` is startup order and it matters: the catalog tables come last so their tabs
sit to the right of the tool views.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from amdockvs.ui.catalog import (
    BINDING_SITES_VIEW_ID,
    COMPLEX_PAIRS_VIEW_ID,
    COMPLEXES_VIEW_ID,
    LIGANDS_VIEW_ID,
    MOLECULES_VIEW_ID,
    RECEPTOR_VIEW_ID,
    SHARDS_VIEW_ID,
)
from amdockvs.ui.catalog.domain_views import LIGAND_ACTIVITY_VIEW_ID
from amdockvs.ui.tools.docking.studio import DOCKING_VIEW_ID
from amdockvs.ui.tools.molecules.build import BUILD_ID
from amdockvs.ui.tools.molecules.diversity import SELECTION_VIEW_ID
from amdockvs.ui.tools.molecules.filter import FILTER_ID
from amdockvs.ui.tools.binding_sites.detection import POCKET_DETECTION_VIEW_ID
from amdockvs.ui.tools.qsar.panels import PREDICTIONS_VIEW_ID, QSAR_MODELS_VIEW_ID


@dataclass(frozen=True)
class ToolEntry:
    """One flat checkable button on the left bar; opens in the tool dock, never as a tab."""

    action_id: str
    title: str
    view_id: str
    icon: str
    order: int


# Order 1 is the (hidden) tools-dock button, so tools start at 2; they share LEFT_BOTTOM,
# which puts an automatic separator between them and the panel toggles (Workflow -1 /
# Details 0, LEFT_TOP).
TOOLS: tuple[ToolEntry, ...] = (
    ToolEntry("tool_filter", "Filter", FILTER_ID, "filter.svg", 2),
    ToolEntry("tool_diversity", "Diversity", SELECTION_VIEW_ID, "diversity.svg", 3),
    ToolEntry("tool_build", "Build", BUILD_ID, "build.svg", 4),
    ToolEntry("tool_pockets", "Pocket Detection", POCKET_DETECTION_VIEW_ID, "binding_site.svg", 5),
    ToolEntry("tool_docking", "Docking Studio", DOCKING_VIEW_ID, "target.svg", 6),
)

TOOL_VIEW_IDS = frozenset(entry.view_id for entry in TOOLS)

# Catalog reference tables live in a top toolbar of checkable actions (checked = tab open),
# separated from the left tool buttons so "pick a table" doesn't collide with "pick a tool".
CATALOG_TABLES = (
    ("Molecules", MOLECULES_VIEW_ID, "catalog.svg"),
    ("Receptors", RECEPTOR_VIEW_ID, "receptor.svg"),
    ("Ligands", LIGANDS_VIEW_ID, "ligands.svg"),
    ("Shards", SHARDS_VIEW_ID, "cloud.svg"),
    ("Binding Sites", BINDING_SITES_VIEW_ID, "binding_site.svg"),
    ("Complexes", COMPLEX_PAIRS_VIEW_ID, "complexes.svg"),
    ("Activity", LIGAND_ACTIVITY_VIEW_ID, "activity.svg"),
)

# Result views: once the data exists in the project it outlives the tool that produced it,
# so these stay reachable with every tool closed. One group per separator.
STANDING_DATA_VIEWS = (
    (
        # Off-target and Redocking are pivots inside this view, not entries of their own:
        # the same results read differently (see tools/docking/results_pivot.py).
        ("Docking Results", COMPLEXES_VIEW_ID, "docking_results.svg"),
    ),
    (
        ("QSAR Models", QSAR_MODELS_VIEW_ID, "models.svg"),
        ("Predictions", PREDICTIONS_VIEW_ID, "predictions.svg"),
    ),
)

# Views opened to look at something and then abandoned. They share one tab slot, so they
# stop accumulating in the tab bar.
PREVIEW_VIEW_IDS = frozenset({QSAR_MODELS_VIEW_ID, BINDING_SITES_VIEW_ID, COMPLEX_PAIRS_VIEW_ID})

# "module:function", each called with the window once the central widget exists.
REGISTRARS: tuple[str, ...] = (
    "amdockvs.ui.monitor:register_monitor_views",
    "amdockvs.ui.catalog:register_molecules_workspace",
    "amdockvs.ui.tools.molecules.build:register_build_workspace",
    "amdockvs.ui.tools.molecules.filter:register_filter_workspace",
    "amdockvs.ui.tools.molecules.diversity:register_selection_workspace",
    "amdockvs.ui.tools.binding_sites.detection:register_pocket_detection_workspace",
    "amdockvs.ui.tools.docking.studio:register_docking_workspace",
    "amdockvs.ui.tools.qsar.panels:register_qsar_panels",
    "amdockvs.ui.tools.workflow_panel:register_workflow_panel",
    "amdockvs.ui.tools.pymol_ribbon:install_pymol_toolbar",
    "amdockvs.ui.catalog:register_receptors_workspace",
    "amdockvs.ui.catalog:register_ligands_workspace",
    "amdockvs.ui.catalog:register_shards_workspace",
    "amdockvs.ui.catalog:register_binding_sites_workspace",
    "amdockvs.ui.catalog:register_complex_pairs_workspace",
    "amdockvs.ui.catalog:register_complexes_workspace",
    "amdockvs.ui.catalog:register_ligand_activity_workspace",
)


def register_all(window) -> None:
    for target in REGISTRARS:
        module_name, _, attr = target.partition(":")
        getattr(import_module(module_name), attr)(window)


if __name__ == "__main__":  # every table entry must name a view that a registrar creates
    assert len(TOOL_VIEW_IDS) == len(TOOLS), "duplicate tool view id"
    for target in REGISTRARS:
        module_name, _, attr = target.partition(":")
        assert callable(getattr(import_module(module_name), attr)), target
    listed = {view_id for _, view_id, _ in CATALOG_TABLES}
    listed |= {view_id for group in STANDING_DATA_VIEWS for _, view_id, _ in group}
    assert not (listed & TOOL_VIEW_IDS), "a tool cannot also be a toolbar data view"
    assert PREVIEW_VIEW_IDS <= listed, "a preview view must be reachable from a toolbar"
    print("ui registry ok")
