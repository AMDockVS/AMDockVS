"""Binding sites: manual boxes, reference ligands, import-time components and P2Rank."""
from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "P2RANK_VERSION": ("amdockvs.binding_sites.p2rank", "P2RANK_VERSION"),
    "P2RankInstallation": ("amdockvs.binding_sites.p2rank", "P2RankInstallation"),
    "P2RankPredictionParams": ("amdockvs.binding_sites.jobs", "P2RankPredictionParams"),
    "BindingSiteAPI": ("amdockvs.binding_sites.api", "BindingSiteAPI"),
    "BindingSiteScope": ("amdockvs.binding_sites.scopes", "BindingSiteScope"),
    "ensure_p2rank": ("amdockvs.binding_sites.p2rank", "ensure_p2rank"),
    "p2rank_prediction_job": ("amdockvs.binding_sites.jobs", "p2rank_prediction_job"),
    "p2rank_status": ("amdockvs.binding_sites.p2rank", "p2rank_status"),
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
