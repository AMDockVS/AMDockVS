from importlib.metadata import PackageNotFoundError, version

from ms_flow.api import AppManifest, ProjectResourceSpec

from amdockvs.core.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    RESOURCE_DOCKING_RESULTS,
    RESOURCE_IMPORT_STATS,
    RESOURCE_POCKET_PREDICTIONS,
    RESOURCE_QSAR_MODELS,
    RESOURCE_SHARDS,
    AMDOCKVS_SCOPE_ID,
)

# A flat project: data folders beside the databases (volumes/ is ms_flow's), cache/ safe to
# delete at any time, tmp/ for job temporaries. Each folder appears on its first write.
AMDOCKVS_PROJECT_RESOURCES = (
    ProjectResourceSpec(key=RESOURCE_SHARDS, relative_path="shards", description="htpvs ligand shards"),
    ProjectResourceSpec(
        key=RESOURCE_DOCKING_RESULTS,
        relative_path="docking",
        description="Campaign result fragments and docking programs' working files",
    ),
    ProjectResourceSpec(key=RESOURCE_QSAR_MODELS, relative_path="models", description="QSAR models"),
    ProjectResourceSpec(
        key=RESOURCE_POCKET_PREDICTIONS,
        relative_path="pockets",
        description="P2Rank outputs; the viewer draws pocket points from them",
    ),
    ProjectResourceSpec(
        key=RESOURCE_IMPORT_STATS,
        relative_path="tmp/import",
        description="Per-batch import tallies, drained when the import ends",
    ),
)

# Distribution metadata is absent when the source tree is used directly through PYTHONPATH.
try:
    _VERSION = version("AMDock-VS")
except PackageNotFoundError:
    _VERSION = "0+unknown"


# Campaign mode is derived from `molecules.storage.library_kind`: a project becomes a campaign
# when it contains shards, which is decided when the screening library is imported.
manifest = AppManifest(
    app_id=AMDOCKVS_APP_ID,
    scope_id=AMDOCKVS_SCOPE_ID,
    name=AMDOCKVS_APP_NAME,
    version=_VERSION,
    description="Docking application built on top of MolSuite.",
    entry_module="amdockvs.app",
    package_name="amdockvs",
    project_resources=AMDOCKVS_PROJECT_RESOURCES,
)
