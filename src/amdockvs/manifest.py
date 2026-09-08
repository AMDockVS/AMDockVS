from importlib.metadata import PackageNotFoundError, version

from ms_flow.api import AppManifest

from amdockvs.core.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    AMDOCKVS_PROJECT_RESOURCES,
    AMDOCKVS_SCOPE_ID,
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
