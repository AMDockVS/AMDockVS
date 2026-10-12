from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "AnalysisResult": ("amdockvs.diversity.api", "AnalysisResult"),
    "CLUSTERING_METHODS": ("amdockvs.diversity.clustering", "CLUSTERING_METHODS"),
    "DiversityAPI": ("amdockvs.diversity.api", "DiversityAPI"),
    "SelectionClusterJobParams": ("amdockvs.diversity.jobs", "SelectionClusterJobParams"),
    "SelectionResult": ("amdockvs.diversity.clustering", "SelectionResult"),
    "cluster_and_select": ("amdockvs.diversity.clustering", "cluster_and_select"),
    "cluster_molecules_job": ("amdockvs.diversity.jobs", "cluster_molecules_job"),
    "cluster_molecules_task": ("amdockvs.diversity.jobs", "cluster_molecules_task"),
    "register_method": ("amdockvs.diversity.clustering", "register_method"),
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
