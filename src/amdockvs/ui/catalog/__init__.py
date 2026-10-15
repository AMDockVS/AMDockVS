from amdockvs.ui.catalog.domain_views import (
    LigandActivityViewWidget,
    register_complexes_workspace,
    register_ligand_activity_workspace,
)
from amdockvs.ui.catalog.complexes import ComplexPairsWidget, register_complex_pairs_workspace
from amdockvs.ui.catalog.binding_sites import BindingSiteWidget, register_binding_sites_workspace
from amdockvs.ui.catalog.ligands import LigandWidget, register_ligands_workspace
from amdockvs.ui.catalog.receptors import ReceptorWidget, register_receptors_workspace
from amdockvs.ui.catalog.molecules import MoleculeWidget, register_molecules_workspace
from amdockvs.ui.catalog.shards import ShardsWidget, register_shards_workspace

__all__ = [
    "BindingSiteWidget",
    "ComplexPairsWidget",
    "LigandWidget",
    "LigandActivityViewWidget",
    "MoleculeWidget",
    "ShardsWidget",
    "ReceptorWidget",
    "register_complex_pairs_workspace",
    "register_binding_sites_workspace",
    "register_complexes_workspace",
    "register_ligands_workspace",
    "register_ligand_activity_workspace",
    "register_molecules_workspace",
    "register_receptors_workspace",
    "register_shards_workspace",
]
