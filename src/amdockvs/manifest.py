from ms_flow.api import AppManifest

from amdockvs.constants import (
    AMDOCKVS_APP_ID,
    AMDOCKVS_APP_NAME,
    AMDOCKVS_PROJECT_RESOURCES,
    AMDOCKVS_SCOPE_ID,
)

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
