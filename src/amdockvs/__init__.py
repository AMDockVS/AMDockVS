from importlib import import_module

_LAZY_EXPORTS = {
    "manifest": ("amdockvs.manifest", "manifest"),
    "MoleculeScope": ("amdockvs.molecules.api", "MoleculeScope"),
    "AMDOCKVS_APP_ID": ("amdockvs.runtime", "AMDOCKVS_APP_ID"),
    "AMDOCKVS_APP_NAME": ("amdockvs.runtime", "AMDOCKVS_APP_NAME"),
    "AMDOCKVS_DEFAULT_PROJECT_DIRS": ("amdockvs.runtime", "AMDOCKVS_DEFAULT_PROJECT_DIRS"),
    "AMDOCKVS_PROJECT_RESOURCES": ("amdockvs.runtime", "AMDOCKVS_PROJECT_RESOURCES"),
    "AMDOCKVS_SCOPE_ID": ("amdockvs.runtime", "AMDOCKVS_SCOPE_ID"),
    "AMDockVSRuntime": ("amdockvs.runtime", "AMDockVSRuntime"),
    "ComplexRef": ("amdockvs.scopes", "ComplexRef"),
    "ComplexSetRef": ("amdockvs.scopes", "ComplexSetRef"),
    "MoleculeSetRef": ("amdockvs.scopes", "MoleculeSetRef"),
    "QSARDatasetRef": ("amdockvs.scopes", "QSARDatasetRef"),
    "QSARModelRef": ("amdockvs.scopes", "QSARModelRef"),
    "DescriptorSummary": ("amdockvs.summaries", "DescriptorSummary"),
    "DockingHitSummary": ("amdockvs.summaries", "DockingHitSummary"),
    "DockingResultsStatsSummary": ("amdockvs.summaries", "DockingResultsStatsSummary"),
    "DockingResultSummary": ("amdockvs.summaries", "DockingResultSummary"),
    "JobStatus": ("amdockvs.summaries", "JobStatus"),
    "LigandSummary": ("amdockvs.summaries", "LigandSummary"),
    "LigandTableStatsSummary": ("amdockvs.summaries", "LigandTableStatsSummary"),
    "ProjectSummary": ("amdockvs.summaries", "ProjectSummary"),
    "ReceptorDockingSummary": ("amdockvs.summaries", "ReceptorDockingSummary"),
    "ReceptorSummary": ("amdockvs.summaries", "ReceptorSummary"),
    "ReceptorTableStatsSummary": ("amdockvs.summaries", "ReceptorTableStatsSummary"),
    "ScreeningSummary": ("amdockvs.summaries", "ScreeningSummary"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value

__all__ = [
    "AMDOCKVS_APP_ID",
    "AMDOCKVS_APP_NAME",
    "AMDOCKVS_DEFAULT_PROJECT_DIRS",
    "AMDOCKVS_PROJECT_RESOURCES",
    "AMDOCKVS_SCOPE_ID",
    "AMDockVSRuntime",
    "ComplexRef",
    "ComplexSetRef",
    "DescriptorSummary",
    "DockingHitSummary",
    "DockingResultsStatsSummary",
    "DockingResultSummary",
    "JobStatus",
    "LigandSummary",
    "LigandTableStatsSummary",
    "MoleculeScope",
    "MoleculeSetRef",
    "ProjectSummary",
    "QSARDatasetRef",
    "QSARModelRef",
    "ReceptorDockingSummary",
    "ReceptorSummary",
    "ReceptorTableStatsSummary",
    "ScreeningSummary",
    "manifest",
]
