from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "MoleculeAPI": ("amdockvs.molecules.api", "MoleculeAPI"),
    "MoleculeScope": ("amdockvs.molecules.api", "MoleculeScope"),
    "MoleculeSetRef": ("amdockvs.molecules.api", "MoleculeSetRef"),
    "ensure_molecule_set_ref": ("amdockvs.molecules.api", "ensure_molecule_set_ref"),
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
