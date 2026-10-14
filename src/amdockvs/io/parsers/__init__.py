from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "count_import_records": ("amdockvs.io.parsers.readers", "count_import_records"),
    "iter_import_entries": ("amdockvs.io.parsers.readers", "iter_import_entries"),
    "iter_raw_records": ("amdockvs.io.parsers.readers", "iter_raw_records"),
    "iter_record_spans": ("amdockvs.io.parsers.readers", "iter_record_spans"),
    "iter_record_span": ("amdockvs.io.parsers.readers", "iter_record_span"),
    "read_record_span": ("amdockvs.io.parsers.readers", "read_record_span"),
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
