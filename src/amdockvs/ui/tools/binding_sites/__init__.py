"""The UI for binding sites: find them, draw the box, pick flexible residues.

Detection is a left-toolbar tool; the box editor and the flexible-residue picker are docks
the Docking Studio raises. All three write through `runtime.binding_sites`.
"""

from amdockvs.ui.tools.binding_sites.box import GridBoxSettingDockWidget
from amdockvs.ui.tools.binding_sites.detection import (
    PocketDetectionWidget,
    register_pocket_detection_workspace,
)
from amdockvs.ui.tools.binding_sites.flexible_residues import FlexibleResiduesPanel

__all__ = [
    "FlexibleResiduesPanel",
    "GridBoxSettingDockWidget",
    "PocketDetectionWidget",
    "register_pocket_detection_workspace",
]
