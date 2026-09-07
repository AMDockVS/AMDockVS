from ms_flow.api import AppManifest

from amdockvs.core.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    AMDOCKVS_PROJECT_RESOURCES,
    AMDOCKVS_SCOPE_ID,
)

# No `project_fields`: the campaign mode used to be one, asked before the project held any data.
# It is derived now (`molecules.storage.library_kind`) — a project is a campaign because it holds
# shards, and that is decided at import, where the library actually is.
manifest = AppManifest(
    app_id=AMDOCKVS_APP_ID,
    scope_id=AMDOCKVS_SCOPE_ID,
    name=AMDOCKVS_APP_NAME,
    version="0.1.0",
    description="Docking application built on top of MolSuite.",
    entry_module="amdockvs.app",
    package_name="amdockvs",
    project_resources=AMDOCKVS_PROJECT_RESOURCES,
)
