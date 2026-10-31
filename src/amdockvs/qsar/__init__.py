from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "DescriptorJobParams": ("amdockvs.qsar.jobs", "DescriptorJobParams"),
    "FittedModel": ("amdockvs.qsar.modeling", "FittedModel"),
    "QSARAPI": ("amdockvs.qsar.api", "QSARAPI"),
    "SUPPORTED_QSAR_ALGORITHMS": ("amdockvs.qsar.modeling", "SUPPORTED_QSAR_ALGORITHMS"),
    "calculate_descriptor_batch": ("amdockvs.qsar.tasks", "calculate_descriptor_batch"),
    "calculate_molecule_alerts_job": ("amdockvs.qsar.jobs", "calculate_molecule_alerts_job"),
    "calculate_molecule_descriptors_job": ("amdockvs.qsar.jobs", "calculate_molecule_descriptors_job"),
    "classification_metrics": ("amdockvs.qsar.modeling", "classification_metrics"),
    "fit_model": ("amdockvs.qsar.modeling", "fit_model"),
    "load_model": ("amdockvs.qsar.modeling", "load_model"),
    "regression_metrics": ("amdockvs.qsar.modeling", "regression_metrics"),
    "SPLITTERS": ("amdockvs.qsar.splitters", "SPLITTERS"),
    "save_model": ("amdockvs.qsar.modeling", "save_model"),
    "supported_algorithms": ("amdockvs.qsar.modeling", "supported_algorithms"),
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
