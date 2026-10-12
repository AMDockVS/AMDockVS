from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "fix_receptor_pdb_file": ("amdockvs.chemistry.tools.receptors", "fix_receptor_pdb_file"),
    "generate_ligand_3d": ("amdockvs.chemistry.tools.ligands", "generate_ligand_3d"),
    "minimize_ligand_molecule": ("amdockvs.chemistry.tools.ligands", "minimize_ligand_molecule"),
    "minimize_receptor_openmm_file": ("amdockvs.chemistry.tools.receptors", "minimize_receptor_openmm_file"),
    "protonate_ligand_molecule": ("amdockvs.chemistry.tools.ligands", "protonate_ligand_molecule"),
    "protonate_receptor_pdb2pqr_file": ("amdockvs.chemistry.tools.receptors", "protonate_receptor_pdb2pqr_file"),
    "protonate_receptor_reduce_file": ("amdockvs.chemistry.tools.receptors", "protonate_receptor_reduce_file"),
    "standardize_ligand_molecule": ("amdockvs.chemistry.tools.ligands", "standardize_ligand_molecule"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = list(_LAZY_EXPORTS)
