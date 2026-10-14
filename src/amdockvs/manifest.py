from importlib.metadata import PackageNotFoundError, version

from ms_flow.api import AppManifest, ProjectResourceSpec

from amdockvs.core.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    RESOURCE_DOCKING_RESULTS,
    RESOURCE_EXPORTS,
    RESOURCE_JOBS,
    RESOURCE_MOLECULES,
    RESOURCE_POCKET_PREDICTIONS,
    RESOURCE_QSAR_MODELS,
    RESOURCE_SHARDS,
    AMDOCKVS_SCOPE_ID,
)

AMDOCKVS_PROJECT_RESOURCES = (
    ProjectResourceSpec(key=RESOURCE_MOLECULES, relative_path="data/molecules", description="General molecule artifacts"),
    ProjectResourceSpec(key=RESOURCE_DOCKING_RESULTS, relative_path="results/docking", description="Docking outputs"),
    ProjectResourceSpec(key=RESOURCE_QSAR_MODELS, relative_path="results/qsar_models", description="QSAR model artifacts"),
    ProjectResourceSpec(
        key=RESOURCE_POCKET_PREDICTIONS,
        relative_path="results/pockets",
        description="Pocket-prediction artifacts",
    ),
    ProjectResourceSpec(key=RESOURCE_EXPORTS, relative_path="exports", description="User exports"),
    ProjectResourceSpec(key=RESOURCE_JOBS, relative_path="jobs", description="App-level job artifacts"),
    ProjectResourceSpec(key=RESOURCE_SHARDS, relative_path="data/shards", description="htpvs ligand shards"),
)
AMDOCKVS_DEFAULT_PROJECT_DIRS = tuple(spec.relative_path for spec in AMDOCKVS_PROJECT_RESOURCES)

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
