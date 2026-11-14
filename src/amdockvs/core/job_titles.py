"""What the Jobs monitor and the job notices call a job: "Docking · gnina (CNN)", not
`amdock_docking_job`. Read from the job's name and the params it was submitted with, so a job
of an earlier session is titled the same way (its params are on record in the journal)."""
from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any, Mapping

_LABELS = {
    "amdock_docking_job": "Docking",
    "amdock_dock_shards_job": "Docking (library)",
    "amdock_redocking_job": "Redocking",
    "amdock_interactions_job": "Interactions",
    "amdock_diagram_job": "Interaction diagrams",
    "amdock_prepare_ligands_job": "Prepare ligands",
    "amdock_prepare_receptors_job": "Prepare receptors",
    "amdock_prepare_ligand_shards_job": "Prepare ligands (library)",
    "amdock_load_molecules_file_job": "Import molecules",
    "amdock_load_ligands_file_job": "Import ligands",
    "amdock_load_ligands_multithreaded_sdf_job": "Import ligands",
    "amdock_load_receptors_file_job": "Import receptors",
    "amdock_shard_ligands_job": "Import ligand library",
    "amdock_pocket_prediction_job": "Pocket prediction",
    "amdock_ligand_chemistry_job": "Ligand chemistry",
    "amdock_receptor_chemistry_job": "Receptor chemistry",
    "amdock_shard_chemistry_job": "Ligand chemistry (library)",
    "amdock_receptor_prediction_job": "Structure prediction",
    "amdock_shard_fingerprints_job": "Fingerprints (library)",
    "amdock_cluster_molecules_job": "Clustering",
    "amdock_calculate_molecule_descriptors_job": "Descriptors",
    "amdock_calculate_molecule_descriptor_alerts_job": "Structural alerts",
    "amdock_qsar_train_job": "QSAR training",
    "amdock_qsar_predict_rows_job": "QSAR prediction",
    "amdock_qsar_predict_shards_job": "QSAR prediction (library)",
}
_DOCKING = {"amdock_docking_job", "amdock_dock_shards_job", "amdock_redocking_job"}
_CHEMISTRY = {"amdock_ligand_chemistry_job", "amdock_receptor_chemistry_job", "amdock_shard_chemistry_job"}


def job_label(name: str) -> str:
    """The kind of job, in words: a job this table does not know reads its name without the
    `amdock_` prefix and `_job` suffix ("Load receptors file")."""
    name = str(name or "")
    if name in _LABELS:
        return _LABELS[name]
    words = re.sub(r"^amdock(vs)?_|_job$", "", name).replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Job"


def _program(engine: str) -> str:
    from amdockvs.docking.engines.programs import get_program_for_engine

    try:
        return get_program_for_engine(engine).label
    except Exception:  # noqa: BLE001 - an engine registered in another session still has a name
        return engine


def _files(params: Mapping[str, Any]) -> str:
    paths = [p for p in (params.get("file_paths") or []) if p] or ([params["file_path"]] if params.get("file_path") else [])
    if not paths:
        return ""
    first = PurePath(str(paths[0])).name
    return first if len(paths) == 1 else f"{first} +{len(paths) - 1}"


def _operations(operation: Any) -> str:
    steps = [operation] if isinstance(operation, str) else list(operation or [])
    names = [str(step[0] if isinstance(step, (list, tuple)) and step else step) for step in steps]
    return ", ".join(name.replace("_", " ") for name in names if name)


def job_title(name: str, params: Any = None) -> str:
    """`job_label(name)` and, from `params`, what tells this job apart from others of its kind:
    the docking program and stage, the files imported, the chemistry steps."""
    label = job_label(name)
    if params is not None and hasattr(params, "model_dump"):
        params = params.model_dump()
    if not isinstance(params, Mapping):
        return label
    detail: list[str] = []
    if name in _DOCKING and params.get("engine"):
        detail.append(_program(str(params["engine"])))
        if params.get("selected_from_receptor_id"):
            detail.append("off-target stage")
    elif name.startswith("amdock_load_"):
        detail.append(_files(params))
    elif name in _CHEMISTRY:
        detail.append(_operations(params.get("operation")))
    elif name == "amdock_receptor_prediction_job" and params.get("model"):
        detail.append(str(params["model"]))
    return " · ".join([label, *(part for part in detail if part)])


__all__ = ["job_label", "job_title"]
