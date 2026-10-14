from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "build_import_graph_payload": ("amdockvs.io.transformers.materializers", "build_import_graph_payload"),
    "materialize_import_batch": ("amdockvs.io.transformers.materializers", "materialize_import_batch"),
    "materialize_multithreaded_sdf_file": ("amdockvs.io.transformers.materializers", "materialize_multithreaded_sdf_file"),
    "offload_source_properties": ("amdockvs.io.transformers.materializers", "offload_source_properties"),
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
