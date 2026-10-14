from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "HitGate": ("amdockvs.screening.materialize", "HitGate"),
    "PAYLOAD_FULL": ("amdockvs.screening.materialize", "PAYLOAD_FULL"),
    "PAYLOAD_LIGHT": ("amdockvs.screening.materialize", "PAYLOAD_LIGHT"),
    "ingest_hits": ("amdockvs.screening.materialize", "ingest_hits"),
    "HTPFilterConfig": ("amdockvs.screening.filters", "HTPFilterConfig"),
    "PROPERTY_KEYS": ("amdockvs.screening.filters", "PROPERTY_KEYS"),
    "evaluate_mol": ("amdockvs.screening.filters", "evaluate_mol"),
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
