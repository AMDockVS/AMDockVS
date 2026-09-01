from amdockvs.ui.catalog.domain_views import (
    COMPLEXES_VIEW_ID,
    LIGAND_ACTIVITY_VIEW_ID,
    # RECEPTOR_VIEW_ID,
    ComplexResultsViewWidget,
    LigandActivityViewWidget,
    register_complexes_workspace,
    register_ligand_activity_workspace,

)
from amdockvs.ui.catalog.complexes import (
    COMPLEX_PAIRS_VIEW_ID,
    ComplexPairsWidget,
    register_complex_pairs_workspace,
)
from amdockvs.ui.catalog.binding_sites import (
    BINDING_SITES_VIEW_ID,
    BindingSiteWidget,
    register_binding_sites_workspace,
)
from amdockvs.ui.catalog.ligands import LIGANDS_VIEW_ID, LigandWidget, register_ligands_workspace
from amdockvs.ui.catalog.receptors import RECEPTOR_VIEW_ID, ReceptorWidget, register_receptors_workspace
from amdockvs.ui.catalog.molecules import MOLECULES_VIEW_ID, MoleculeWidget, register_molecules_workspace

__all__ = [
    "COMPLEXES_VIEW_ID",
    "COMPLEX_PAIRS_VIEW_ID",
    "BINDING_SITES_VIEW_ID",
    "BindingSiteWidget",
    "ComplexPairsWidget",
    "ComplexResultsViewWidget",
    "LIGANDS_VIEW_ID",
    "LIGAND_ACTIVITY_VIEW_ID",
    "LigandWidget",
    "LigandActivityViewWidget",
    "MOLECULES_VIEW_ID",
    "MoleculeWidget",
    "RECEPTOR_VIEW_ID",
    "ReceptorWidget",
    "register_complex_pairs_workspace",
    "register_binding_sites_workspace",
    "register_complexes_workspace",
    "register_ligands_workspace",
    "register_ligand_activity_workspace",
    "register_molecules_workspace",
    "register_receptors_workspace",
]
