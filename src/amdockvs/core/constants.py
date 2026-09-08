from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ms_flow.api import ProjectResourceSpec

from amdockvs.core.configuration import app_config

AMDOCKVS_APP_ID = "amdockvs"
AMDOCKVS_SCOPE_ID = "docking"
AMDOCKVS_APP_NAME = "AMDockVS"
RESOURCE_MOLECULES = "molecules"
RESOURCE_DOCKING_RESULTS = "docking_results"
RESOURCE_QSAR_MODELS = "qsar_models"
RESOURCE_POCKET_PREDICTIONS = "pocket_predictions"
RESOURCE_EXPORTS = "exports"
RESOURCE_JOBS = "jobs"
RESOURCE_SHARDS = "shards"

TABLE_MOLECULES = "molecules"
TABLE_MOLECULE_REPRESENTATIONS = "molecule_representations"
TABLE_MOLECULE_MODELS = "molecule_models"
TABLE_MOLECULE_SOURCE_PROPERTIES = "molecule_source_properties"
TABLE_BINDING_SITES = "binding_sites"
TABLE_ENGINES = "engines"
TABLE_DOCKING_RESULTS = "docking_results"
TABLE_CONSENSUS_SCORES = "consensus_scores"
TABLE_INTERACTION_RESULTS = "interaction_results"
TABLE_DESCRIPTOR_SCHEMAS = "descriptor_schemas"
TABLE_DESCRIPTOR_VECTORS = "descriptor_vector_records"
TABLE_DESCRIPTOR_BLOCKS = "descriptor_blocks"
TABLE_FINGERPRINTS = "fingerprint_records"
TABLE_MOLECULE_SETS = "molecule_sets"
TABLE_MOLECULE_SET_MEMBERS = "molecule_set_members"
TABLE_LIGAND_ACTIVITIES = "ligand_activities"
TABLE_COMPLEXES = "complexes"
TABLE_QSAR_DATASETS = "qsar_datasets"
TABLE_QSAR_DATASET_ITEMS = "qsar_dataset_items"
TABLE_QSAR_MODELS = "qsar_models"
TABLE_QSAR_PREDICTIONS = "qsar_predictions"
TABLE_SIMILARITY_RESULTS = "similarity_results"
TABLE_CLUSTERING_RESULTS = "clustering_results"
TABLE_SCREENING_SHARDS = "screening_shards"
TABLE_SHARD_ENGINE_STATES = "shard_engine_states"
TABLE_SCREENING_SHARD_RUNS = "screening_shard_runs"
TABLE_SCREENING_DISPATCHES = "screening_dispatches"
TABLE_SCREENING_TARGETS = "screening_targets"

# Deprecated aliases kept temporarily while non-model consumers are migrated.
TABLE_REPRESENTATIONS = TABLE_MOLECULE_REPRESENTATIONS
TABLE_SETS = TABLE_MOLECULE_SETS
TABLE_SET_ITEMS = TABLE_MOLECULE_SET_MEMBERS
TABLE_DESCRIPTORS = TABLE_DESCRIPTOR_VECTORS
TABLE_RESULTS = TABLE_DOCKING_RESULTS
TABLE_ACTIVITIES = TABLE_LIGAND_ACTIVITIES


AMDOCKVS_PROJECT_RESOURCES = (
    ProjectResourceSpec(key=RESOURCE_MOLECULES, relative_path="data/molecules", description="General molecule artifacts"),
    ProjectResourceSpec(key=RESOURCE_DOCKING_RESULTS, relative_path="results/docking", description="Docking outputs"),
    ProjectResourceSpec(key=RESOURCE_QSAR_MODELS, relative_path="results/qsar_models", description="QSAR model artifacts"),
    ProjectResourceSpec(
        key=RESOURCE_POCKET_PREDICTIONS,
        relative_path="results/pockets",
        description="Pocket-prediction artifacts",
    ),
    ProjectResourceSpec(key=RESOURCE_EXPORTS, relative_path="exports", description="User exports"),
    ProjectResourceSpec(key=RESOURCE_JOBS, relative_path="jobs", description="App-level job artifacts"),
    ProjectResourceSpec(key=RESOURCE_SHARDS, relative_path="data/shards", description="htpvs ligand shards"),
)
AMDOCKVS_DEFAULT_PROJECT_DIRS = tuple(spec.relative_path for spec in AMDOCKVS_PROJECT_RESOURCES)

STATUS_FLAG_PAINS = 1 << 0
STATUS_FLAG_RO5_VIOLATION = 1 << 1


# MolSuite exposes a single logical CPU executor named "compute" (loky locally,
# ray when a cluster is configured). AMDockVS no longer picks a local backend
# variant — the backend is switched at the MolSuite layer, not per job.
DEFAULT_LOCAL_CPU_EXECUTOR = "compute"


def vina_command(runtime=None) -> str:
    """The vina executable: the configured path, else a sibling of this interpreter, else PATH.

    Resolved on demand rather than frozen at import, so pointing Settings at another build
    takes effect without restarting.
    """
    configured = str(app_config(runtime).external_tools.vina_path or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    sibling = Path(sys.executable).expanduser().resolve().parent / "vina"
    if sibling.exists():
        return str(sibling)
    return shutil.which("vina") or "vina"


def vina_backend(runtime=None) -> str:
    """`binary` when the executable is really there, `python` (the bindings) otherwise."""
    return "binary" if Path(vina_command(runtime)).expanduser().exists() else "python"


# Module-level defaults for signatures evaluated at import time; call the functions above
# wherever a runtime is available.
DEFAULT_VINA_COMMAND = vina_command()
DEFAULT_VINA_BACKEND = vina_backend()

AMDOCKVS_LOCAL_EXECUTORS = ("thread", "compute")
AMDOCKVS_PROCESS_EXECUTORS = ("compute",)
