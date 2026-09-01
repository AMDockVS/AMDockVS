from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ms_flow.api import ProjectResourceSpec

AMDOCKVS_APP_ID = "amdockvs"
AMDOCKVS_SCOPE_ID = "docking"
AMDOCKVS_APP_NAME = "AMDockVS"
RESOURCE_MOLECULES = "molecules"
RESOURCE_DOCKING_RESULTS = "docking_results"
RESOURCE_QSAR_MODELS = "qsar_models"
RESOURCE_POCKET_PREDICTIONS = "pocket_predictions"
RESOURCE_EXPORTS = "exports"
RESOURCE_JOBS = "jobs"

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

# Deprecated aliases kept temporarily while non-model consumers are migrated.
TABLE_REPRESENTATIONS = TABLE_MOLECULE_REPRESENTATIONS
TABLE_SETS = TABLE_MOLECULE_SETS
TABLE_SET_ITEMS = TABLE_MOLECULE_SET_MEMBERS
TABLE_DESCRIPTORS = TABLE_DESCRIPTOR_VECTORS
TABLE_RESULTS = TABLE_DOCKING_RESULTS
TABLE_ACTIVITIES = TABLE_LIGAND_ACTIVITIES
TABLE_LIGANDS = "ligands"
TABLE_RECEPTORS = "receptors"
VIEW_LIGANDS = "ligand_inventory"
VIEW_RECEPTORS = "receptor_inventory"

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
)
AMDOCKVS_DEFAULT_PROJECT_DIRS = tuple(spec.relative_path for spec in AMDOCKVS_PROJECT_RESOURCES)

DEFAULT_LOAD_BATCH_SIZE = 1000
DEFAULT_DESCRIPTOR_BATCH_SIZE = 1000

STATUS_FLAG_PAINS = 1 << 0
STATUS_FLAG_RO5_VIOLATION = 1 << 1


# MolSuite exposes a single logical CPU executor named "compute" (loky locally,
# ray when a cluster is configured). AMDockVS no longer picks a local backend
# variant — the backend is switched at the MolSuite layer, not per job.
DEFAULT_LOCAL_CPU_EXECUTOR = "compute"


def _default_vina_command() -> str:
    executable_dir = Path(sys.executable).expanduser().resolve().parent
    sibling_vina = executable_dir / "vina"
    if sibling_vina.exists():
        return str(sibling_vina)
    return shutil.which("vina") or "vina"


DEFAULT_VINA_COMMAND = _default_vina_command()
DEFAULT_VINA_BACKEND = "binary" if Path(DEFAULT_VINA_COMMAND).expanduser().exists() else "python"
# TODO: evaluate an adaptive batch_size based on real load and docking backend.
DEFAULT_DOCKING_BATCH_SIZE = 4

AMDOCKVS_LOCAL_EXECUTORS = ("thread", "compute")
AMDOCKVS_PROCESS_EXECUTORS = ("compute",)
