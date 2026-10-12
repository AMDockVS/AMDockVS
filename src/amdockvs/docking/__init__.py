from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "DockingAPI": ("amdockvs.docking.api", "DockingAPI"),
    "DiagramJobParams": ("amdockvs.docking.results.jobs", "DiagramJobParams"),
    "DiagramJobSpec": ("amdockvs.docking.results.jobs", "DiagramJobSpec"),
    "DockingJobParams": ("amdockvs.docking.jobs", "DockingJobParams"),
    "DockingJobSpec": ("amdockvs.docking.jobs", "DockingJobSpec"),
    "DockingEngineConfig": ("amdockvs.docking.engines.programs", "DockingEngineConfig"),
    "DockingProgramSpec": ("amdockvs.docking.engines.programs", "DockingProgramSpec"),
    "DockingProtocol": ("amdockvs.docking.planning", "DockingProtocol"),
    "DockingReadiness": ("amdockvs.docking.readiness", "DockingReadiness"),
    "DockingReadinessService": ("amdockvs.docking.readiness", "DockingReadinessService"),
    "DockingRunIdentity": ("amdockvs.docking.planning", "DockingRunIdentity"),
    "DockingRunRequest": ("amdockvs.docking.planning", "DockingRunRequest"),
    "DockingSubmission": ("amdockvs.docking.submission", "DockingSubmission"),
    "DockingSubmissionService": ("amdockvs.docking.submission", "DockingSubmissionService"),
    "DockingVinaJobSpec": ("amdockvs.docking.jobs", "DockingVinaJobSpec"),
    "InteractionJobParams": ("amdockvs.docking.results.jobs", "InteractionJobParams"),
    "InteractionJobSpec": ("amdockvs.docking.results.jobs", "InteractionJobSpec"),
    "PreparationJobParams": ("amdockvs.docking.preparation.jobs", "PreparationJobParams"),
    "PreparationProfile": ("amdockvs.docking.preparation.profiles", "PreparationProfile"),
    "PrepareLigandsJobSpec": ("amdockvs.docking.preparation.jobs", "PrepareLigandsJobSpec"),
    "PrepareReceptorsJobSpec": ("amdockvs.docking.preparation.jobs", "PrepareReceptorsJobSpec"),
    "RedockingJobParams": ("amdockvs.docking.jobs", "RedockingJobParams"),
    "RedockingJobSpec": ("amdockvs.docking.jobs", "RedockingJobSpec"),
    "RedockingVinaJobSpec": ("amdockvs.docking.jobs", "RedockingVinaJobSpec"),
    "build_docking_pair": ("amdockvs.docking.pairs", "build_docking_pair"),
    "collect_interaction_rows": ("amdockvs.docking.results.interactions", "collect_interaction_rows"),
    "diagram_job": ("amdockvs.docking.results.jobs", "diagram_job"),
    "docking_input_path_from_row": ("amdockvs.docking.preparation.state", "docking_input_path_from_row"),
    "docking_job": ("amdockvs.docking.jobs", "docking_job"),
    "docking_signature": ("amdockvs.docking.planning", "docking_signature"),
    "interactions_job": ("amdockvs.docking.results.jobs", "interactions_job"),
    "get_receptor_metadata_json": ("amdockvs.docking.repository", "get_receptor_metadata_json"),
    "get_docking_program": ("amdockvs.docking.engines.programs", "get_docking_program"),
    "get_preparation_profile": ("amdockvs.docking.preparation.profiles", "get_preparation_profile"),
    "grid_from_metadata_json": ("amdockvs.docking.preparation.state", "grid_from_metadata_json"),
    "grid_from_row": ("amdockvs.docking.preparation.state", "grid_from_row"),
    "iter_docking_batches_from_rows": ("amdockvs.docking.pairs", "iter_docking_batches_from_rows"),
    "list_entity_rows": ("amdockvs.docking.repository", "list_entity_rows"),
    "list_docking_programs": ("amdockvs.docking.engines.programs", "list_docking_programs"),
    "list_preparation_profiles": ("amdockvs.docking.preparation.profiles", "list_preparation_profiles"),
    "list_receptor_ids_in_set": ("amdockvs.docking.repository", "list_receptor_ids_in_set"),
    "merge_grid_metadata": ("amdockvs.docking.preparation.state", "merge_grid_metadata"),
    "merge_prepared_metadata": ("amdockvs.docking.preparation.state", "merge_prepared_metadata"),
    "persist_prepared_updates": ("amdockvs.docking.repository", "persist_prepared_updates"),
    "prepare_entities_rows": ("amdockvs.docking.preparation.entities", "prepare_entities_rows"),
    "prepare_ligands_job": ("amdockvs.docking.preparation.jobs", "prepare_ligands_job"),
    "prepare_receptors_job": ("amdockvs.docking.preparation.jobs", "prepare_receptors_job"),
    "prepared_path_from_row": ("amdockvs.docking.preparation.state", "prepared_path_from_row"),
    "project_db_path": ("amdockvs.docking.repository", "project_db_path"),
    "protocol_job_key": ("amdockvs.docking.planning", "protocol_job_key"),
    "redocking_job": ("amdockvs.docking.jobs", "redocking_job"),
    "register_dock_runner": ("amdockvs.docking.engines.registry", "register_dock_runner"),
    "register_docking_engine": ("amdockvs.docking.engines.registry", "register_docking_engine"),
    "register_docking_program": ("amdockvs.docking.engines.programs", "register_docking_program"),
    "register_preparation_profile": ("amdockvs.docking.preparation.profiles", "register_preparation_profile"),
    "resolve_docking_output_dir": ("amdockvs.docking.repository", "resolve_docking_output_dir"),
    "resolve_storage_dir": ("amdockvs.docking.repository", "resolve_storage_dir"),
    "run_vina_docking_rows": ("amdockvs.docking.engines.vina", "run_vina_docking_rows"),
    "run_docking_chunk": ("amdockvs.docking.engines.registry", "run_docking_chunk"),
    "update_receptor_metadata_json": ("amdockvs.docking.repository", "update_receptor_metadata_json"),
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
