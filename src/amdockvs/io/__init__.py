from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "ImportBatchPayload": ("amdockvs.io.payloads", "ImportBatchPayload"),
    "ImportPrefilterPolicy": ("amdockvs.io.payloads", "ImportPrefilterPolicy"),
    "LoaderAPI": ("amdockvs.io.api", "LoaderAPI"),
    "LoadFileParams": ("amdockvs.io.jobs", "LoadFileParams"),
    "LoadMultithreadedSDFParams": ("amdockvs.io.jobs", "LoadMultithreadedSDFParams"),
    "MultithreadedSDFImportPayload": ("amdockvs.io.payloads", "MultithreadedSDFImportPayload"),
    "build_import_graph_payload": ("amdockvs.io.transformers", "build_import_graph_payload"),
    "count_import_records": ("amdockvs.io.parsers", "count_import_records"),
    "estimate_import_chunks": ("amdockvs.io.jobs", "estimate_import_chunks"),
    "load_ligands_file_job": ("amdockvs.io.jobs", "load_ligands_file_job"),
    "load_ligands_multithreaded_sdf_job": ("amdockvs.io.jobs", "load_ligands_multithreaded_sdf_job"),
    "load_receptors_file_job": ("amdockvs.io.jobs", "load_receptors_file_job"),
    "materialize_import_rows": ("amdockvs.io.jobs", "materialize_import_rows"),
    "materialize_import_batch": ("amdockvs.io.transformers", "materialize_import_batch"),
    "materialize_multithreaded_sdf_rows": ("amdockvs.io.jobs", "materialize_multithreaded_sdf_rows"),
    "materialize_multithreaded_sdf_file": ("amdockvs.io.transformers", "materialize_multithreaded_sdf_file"),
    "stream_import_payload_batches": ("amdockvs.io.loaders", "stream_import_payload_batches"),
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
