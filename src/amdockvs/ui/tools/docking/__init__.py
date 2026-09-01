from amdockvs.ui.tools.docking.flexible_residues import FlexibleResiduesPanel
from amdockvs.ui.tools.docking.preparation_panel import EngineStatePrepView, PreparationPanel
from amdockvs.ui.tools.docking.protocol_editor import ProtocolEditorWidget
from amdockvs.ui.tools.docking.run_panel import RunPanel
from amdockvs.ui.tools.docking.scope_panel import ScopePanel
from amdockvs.ui.tools.docking.studio import (
    DOCKING_VIEW_ID,
    PREP_STATUS_VIEW_ID,
    DockingStudioWidget,
    register_docking_workspace,
)

__all__ = [
    "DOCKING_VIEW_ID",
    "PREP_STATUS_VIEW_ID",
    "DockingStudioWidget",
    "EngineStatePrepView",
    "FlexibleResiduesPanel",
    "PreparationPanel",
    "ProtocolEditorWidget",
    "RunPanel",
    "ScopePanel",
    "register_docking_workspace",
]
