"""The id of every view and tool the main window registers.

Plain strings with no imports, so any module — Qt-free ones such as `workflows.steps`
included — can name a view without importing the widget that implements it. Where each one
is placed in the window is `ui.registry`'s business.
"""

# Catalog tables
MOLECULES_VIEW_ID = "workspace.molecules"
RECEPTOR_VIEW_ID = "workspace.receptors"
LIGANDS_VIEW_ID = "workspace.ligands"
SHARDS_VIEW_ID = "workspace.shards"
BINDING_SITES_VIEW_ID = "workspace.binding_sites"
COMPLEX_PAIRS_VIEW_ID = "workspace.complex_pairs"
LIGAND_ACTIVITY_VIEW_ID = "workspace.ligand_activity"

# Result views
COMPLEXES_VIEW_ID = "workspace.complexes"  # Docking Results
OFFTARGET_VIEW_ID = "workspace.offtarget"
REDOCKING_VIEW_ID = "workspace.redocking"
CLUSTERS_VIEW_ID = "workspace.clusters"
QSAR_MODELS_VIEW_ID = "workspace.qsar_models"
PREDICTIONS_VIEW_ID = "workspace.qsar_predictions"

# Tools
FILTER_ID = "moltools.filter"
SELECTION_VIEW_ID = "moltools.diversity"
BUILD_ID = "moltools.build"
POCKET_DETECTION_VIEW_ID = "moltools.pocket_detection"
DOCKING_VIEW_ID = "workspace.docking"
QSAR_BUILD_VIEW_ID = "workspace.qsar_build"

# Auxiliary panes and panels
PREP_STATUS_VIEW_ID = "workspace.prep_status"
POCKET_SITES_VIEW_ID = "moltools.binding_sites"
WORKFLOW_VIEW_ID = "workspace.workflow"
