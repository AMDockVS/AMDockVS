"""Names and literals, and nothing else at import: every worker loads this module.

What needs the settings or the project API is built when first asked for (see `__getattr__`).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

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
TABLE_PAYLOADS = "ms_payloads"  # ms_flow's; app columns `<role>_payload_id` reference it
# Payload series: payloads with the same lifetime share volumes (ms_flow.payloads).
SERIES_MOLECULES = "molecules"  # ligand structures, stored + current, and their models
SERIES_RECEPTORS = "receptors"  # receptor structures, stored + current, and their models
SERIES_PREPARED = "prepared"  # engine inputs (PDBQT), re-made when the structure changes
SERIES_POSES = "poses"  # docking poses (SDF)
SERIES_REFERENCES = "references"  # frozen complex references, never rewritten
SERIES_AUX = "aux"  # sidecars (ESMFold PAE)
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
TABLE_SHARD_GENERATIONS = "shard_generations"
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
    from amdockvs.core.configuration import app_config

    configured = str(app_config(runtime).docking.vina.path or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    sibling = Path(sys.executable).expanduser().resolve().parent / "vina"
    if sibling.exists():
        return str(sibling)
    return shutil.which("vina") or "vina"


def vina_backend(runtime=None) -> str:
    """`binary` when the executable is really there, `python` (the bindings) otherwise."""
    return "binary" if Path(vina_command(runtime)).expanduser().exists() else "python"


def __getattr__(name: str):
    """`DEFAULT_VINA_COMMAND` and `DEFAULT_VINA_BACKEND`: defaults for signatures, read from the
    settings by whoever asks for them, which a module that only runs a task does not."""
    if name == "DEFAULT_VINA_COMMAND":
        value = vina_command()
    elif name == "DEFAULT_VINA_BACKEND":
        value = vina_backend()
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


AMDOCKVS_LOCAL_EXECUTORS = ("thread", "compute")
AMDOCKVS_PROCESS_EXECUTORS = ("compute",)

# Jobs whose worker only touches its payload and the paths staged with it — no project.db,
# no GUI state — can run off this machine. "hpc" is a *backend*, not a name: the executor is
# registered under whatever the user called the worker, and MF matches the backend when the
# name is not in this tuple. An HPC chunk costs no local CPU token
# (`hpc_adapter.consumes_local_cpu_tokens is False`), so a cluster run and a local run
# proceed at the same time instead of taking turns.
AMDOCKVS_OFFLOADABLE_EXECUTORS = ("thread", "compute", "hpc")
