"""The id of every view and tool the main window registers.

Plain strings with no imports, so any module — Qt-free ones such as `workflows.steps`
included — can name a view without importing the widget that implements it. Where each one
is placed in the window is `ui.registry`'s business.
"""

# Catalog tables
MOLECULES_VIEW_ID = "molecules"
RECEPTOR_VIEW_ID = "receptors"
LIGANDS_VIEW_ID = "ligands"
SHARDS_VIEW_ID = "shards"
BINDING_SITES_VIEW_ID = "binding_sites"
COMPLEX_PAIRS_VIEW_ID = "complex_pairs"
LIGAND_ACTIVITY_VIEW_ID = "ligand_activity"

# Result views
COMPLEXES_VIEW_ID = "docking_results"  # the "Docking Results" tab
OFFTARGET_VIEW_ID = "offtarget"
REDOCKING_VIEW_ID = "redocking"
CLUSTERS_VIEW_ID = "clusters"
QSAR_ANALYSIS_VIEW_ID = "qsar_analysis"  # Model analysis | Result analysis

# Tools
FILTER_ID = "filter"
SELECTION_VIEW_ID = "diversity"
BUILD_ID = "build"
POCKET_DETECTION_VIEW_ID = "pocket_detection"
DOCKING_VIEW_ID = "docking"
QSAR_BUILD_VIEW_ID = "qsar_build"

# Auxiliary panes and panels
PREP_STATUS_VIEW_ID = "prep_status"
POCKET_SITES_VIEW_ID = "pocket_sites"
WORKFLOW_VIEW_ID = "workflow"
