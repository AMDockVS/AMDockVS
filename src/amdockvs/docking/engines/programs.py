from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from amdockvs.workflows.rules import apply_workflow_filters


class DockingEngineConfig(BaseModel):
    """Validated configuration passed unchanged from API to an engine runner.

    Built-ins use the Vina-family fields below. A new program may supply its own
    Pydantic model without adding fields to the shared Molsuite job contract.
    """

    model_config = ConfigDict(extra="allow")

    exhaustiveness: int = Field(default=8, ge=1)
    num_modes: int = Field(default=9, ge=1)
    scoring_function: str = "vina"
    vina_backend: str = "python"
    vina_command: str = "vina"
    vina_cpu: int = Field(default=1, ge=1)
    seed: int = 0
    spacing: float = Field(default=0.375, gt=0.0)
    energy_range: float = Field(default=3.0, ge=0.0)
    min_rmsd: float = Field(default=1.0, ge=0.0)


class GninaEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exhaustiveness: int = Field(default=8, ge=1, le=256)
    num_modes: int = Field(default=9, ge=1, le=128)
    vina_cpu: int = Field(default=1, ge=1, le=128, title="CPU per task")
    scoring_function: Literal["rescore", "none", "refinement", "all"] = "rescore"
    seed: int = 0


class QVinaEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exhaustiveness: int = Field(default=8, ge=1, le=256)
    num_modes: int = Field(default=9, ge=1, le=128)
    vina_cpu: int = Field(default=1, ge=1, le=128, title="CPU per task")
    energy_range: float = Field(default=3.0, ge=0.0, title="Energy range (kcal/mol)")
    seed: int = 0


class AutoDockGPUEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nrun: int = Field(default=20, ge=1, le=1000, title="Genetic algorithm runs")
    num_modes: int = Field(default=9, ge=1, le=128, title="Poses kept (cluster leads)")
    # Read by autogrid4 when it builds the maps; empty keeps the built-in AutoDock4 parameters.
    parameter_file: str = Field(
        default="",
        title="Parameter file (.dat)",
        description="AutoDock4 parameter file with modified or added atom types. Empty uses the built-in parameters.",
        json_schema_extra={"format": "file-path"},
    )
    derived_types: str = Field(
        default="",
        title="Derived atom types",
        description="Atom types added by the parameter file and the known type each one derives from, e.g. XX=OA/C1,C2=C.",
    )
    seed: int = 0


@dataclass(frozen=True)
class DockingProgramSpec:
    key: str
    label: str
    workflow_key: str
    preparation_engine: str
    docking_engine: str
    receptor_types: tuple[str, ...] = ("protein",)
    ligand_types: tuple[str, ...] = ("small_molecule",)
    experiment_kinds: tuple[str, ...] = ("docking", "redocking")
    requires_binding_site: bool = True
    # DiffDock docks from the SMILES; everything that goes through Meeko needs coordinates.
    requires_ligand_3d: bool = True
    requires_ligand_preparation: bool = True
    requires_receptor_preparation: bool = True
    supports_shards: bool = False
    config_model: type[BaseModel] = DockingEngineConfig
    protocol_identity_keys: tuple[str, ...] = ("scoring_function", "exhaustiveness")
    gpu_scoring_functions: tuple[str, ...] = ()
    resource_resolver: Callable[[Mapping[str, Any]], Mapping[str, int]] | None = None

    def supports(
        self,
        *,
        receptor_type: str | None = None,
        ligand_type: str | None = None,
        experiment_kind: str | None = None,
    ) -> bool:
        receptor = str(receptor_type or "protein").strip().lower()
        ligand = str(ligand_type or "small_molecule").strip().lower()
        experiment = str(experiment_kind or "docking").strip().lower()
        return (
            receptor in {value.lower() for value in self.receptor_types}
            and ligand in {value.lower() for value in self.ligand_types}
            and experiment in {value.lower() for value in self.experiment_kinds}
        )

    def entity_filters(self, scope: Mapping[str, object] | None, *, role: str) -> dict[str, object]:
        filters = dict(scope or {})
        filters.setdefault("excluded", False)
        filters = apply_workflow_filters(filters, workflow=self.workflow_key, role=role)
        return filters

    @property
    def prepared_flag_key(self) -> str:
        return f"prepared_{self.preparation_engine}"

    @property
    def prepared_path_key(self) -> str:
        return f"{self.prepared_flag_key}_path"

    @property
    def grid_flag_key(self) -> str:
        return f"grid_{self.preparation_engine}"

    @property
    def grid_payload_key(self) -> str:
        return f"{self.grid_flag_key}_payload"

    def operation_name(self, action: str) -> str:
        return f"docking.{self.key}.{action}"

    def validate_config(self, value: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.config_model.model_validate(dict(value or {})).model_dump(mode="python")

    def resource_requirements(self, value: Mapping[str, Any] | None = None) -> dict[str, int]:
        config = self.validate_config(value)
        if self.resource_resolver is not None:
            resources = {
                str(key): max(0, int(amount))
                for key, amount in self.resource_resolver(config).items()
            }
            resources.setdefault("cpu_required", 1)
            return resources
        resources = {
            "cpu_required": max(1, int(config.get("cpu_required") or config.get("vina_cpu") or 1))
        }
        scoring = str(config.get("scoring_function") or "").strip().lower()
        if scoring in {item.lower() for item in self.gpu_scoring_functions}:
            resources["gpu_required"] = 1
        return resources

    def required_jobs(self) -> dict[str, str]:
        jobs: dict[str, str] = {}
        if self.requires_ligand_preparation:
            jobs["ligands_prepared"] = f"runtime.docking.prepare_ligands(program={self.key!r}, ...)"
        if self.requires_receptor_preparation:
            jobs["receptors_prepared"] = f"runtime.docking.prepare_receptors(program={self.key!r}, ...)"
        if self.requires_binding_site:
            jobs["receptor_binding_sites"] = "Define and activate a receptor binding site first."
        return jobs


@dataclass(frozen=True)
class AutoDockLikeProgram(DockingProgramSpec):
    def __init__(
        self,
        *,
        key: str = "autodock",
        label: str = "AutoDock-like Docking",
        workflow_key: str = "vina",
        preparation_engine: str = "ad4",
        docking_engine: str = "vina",
        config_model: type[BaseModel] = DockingEngineConfig,
        protocol_identity_keys: tuple[str, ...] = ("scoring_function", "exhaustiveness"),
        gpu_scoring_functions: tuple[str, ...] = (),
        resource_resolver: Callable[[Mapping[str, Any]], Mapping[str, int]] | None = None,
    ):
        super().__init__(
            key=key,
            label=label,
            workflow_key=workflow_key,
            preparation_engine=preparation_engine,
            docking_engine=docking_engine,
            receptor_types=("protein",),
            ligand_types=("small_molecule",),
            experiment_kinds=("docking", "redocking"),
            requires_binding_site=True,
            requires_ligand_preparation=True,
            requires_receptor_preparation=True,
            supports_shards=True,
            config_model=config_model,
            protocol_identity_keys=protocol_identity_keys,
            gpu_scoring_functions=gpu_scoring_functions,
            resource_resolver=resource_resolver,
        )


@dataclass(frozen=True)
class VinaProgram(AutoDockLikeProgram):
    scoring_functions: tuple[str, ...] = ("vina", "vinardo", "ad4")

    def __init__(self):
        super().__init__(
            key="vina",
            label="AutoDock Vina",
            workflow_key="vina",
            preparation_engine="ad4",
            docking_engine="vina",
        )


@dataclass(frozen=True)
class GninaProgram(AutoDockLikeProgram):
    # gnina reuses the Vina PDBQT prep; its "scoring_functions" are the --cnn_scoring modes
    # (rescore default). refinement/all are GPU-bound — docking/api.py declares the GPU token.
    scoring_functions: tuple[str, ...] = ("rescore", "none", "refinement", "all")

    def __init__(self):
        super().__init__(
            key="gnina",
            label="gnina (CNN)",
            workflow_key="vina",
            preparation_engine="ad4",
            docking_engine="gnina",
            config_model=GninaEngineConfig,
            gpu_scoring_functions=("refinement", "all"),
        )


VINA_PROGRAM = VinaProgram()
GNINA_PROGRAM = GninaProgram()
AUTODOCK_PROGRAM = AutoDockLikeProgram(
    key="autodock",
    label="AutoDock-like Docking",
    workflow_key="vina",
    preparation_engine="ad4",
    docking_engine="vina",
)

# AutoDock-GPU shares the Vina PDBQT preparation and docks with the AutoDock4 force field
# over autogrid4 maps. Always one GPU token per chunk: a single process saturates the card.
# ponytail: the parameter file is identified by its path; editing it in place keeps the
# protocol identity. Hash its content into the identity if that ever mixes results.
# See docking/engines/adgpu.py.
ADGPU_PROGRAM = AutoDockLikeProgram(
    key="adgpu",
    label="AutoDock-GPU",
    workflow_key="vina",
    preparation_engine="ad4",
    docking_engine="adgpu",
    config_model=AutoDockGPUEngineConfig,
    protocol_identity_keys=("nrun", "parameter_file", "derived_types"),
    resource_resolver=lambda _config: {"cpu_required": 1, "gpu_required": 1},
)

# QuickVina 2 shares the Vina PDBQT preparation and the Vina scoring function; it only
# searches faster, so the scoring function is not part of its identity. See docking/engines/qvina.py.
QVINA_PROGRAM = AutoDockLikeProgram(
    key="qvina",
    label="QuickVina 2",
    workflow_key="vina",
    preparation_engine="ad4",
    docking_engine="qvina",
    config_model=QVinaEngineConfig,
    protocol_identity_keys=("exhaustiveness",),
)

_PROGRAMS: dict[str, DockingProgramSpec] = {
    VINA_PROGRAM.key: VINA_PROGRAM,
    QVINA_PROGRAM.key: QVINA_PROGRAM,
    GNINA_PROGRAM.key: GNINA_PROGRAM,
    ADGPU_PROGRAM.key: ADGPU_PROGRAM,
}

_PROGRAM_ALIASES: dict[str, str] = {
    "autodock": VINA_PROGRAM.key,
    "autodock_like": VINA_PROGRAM.key,
}


def list_docking_programs() -> tuple[DockingProgramSpec, ...]:
    return tuple(_PROGRAMS.values())


def register_docking_program(program: DockingProgramSpec, *, replace: bool = False) -> None:
    key = str(program.key or "").strip().lower()
    if not key:
        raise ValueError("A docking program requires a non-empty key.")
    if key in _PROGRAMS and not replace:
        raise ValueError(f"Docking program '{key}' is already registered.")
    _PROGRAMS[key] = program


def get_docking_program(value: str | None) -> DockingProgramSpec:
    normalized = str(value or "").strip().lower() or VINA_PROGRAM.key
    normalized = _PROGRAM_ALIASES.get(normalized, normalized)
    program = _PROGRAMS.get(normalized)
    if program is None:
        supported = ", ".join(sorted(_PROGRAMS))
        raise ValueError(f"Unsupported docking program '{value}'. Supported programs: {supported}")
    return program


def get_program_for_engine(engine: str) -> DockingProgramSpec:
    normalized = str(engine or "").strip().lower()
    for program in _PROGRAMS.values():
        if program.docking_engine.lower() == normalized:
            return program
    raise ValueError(f"No docking program is registered for engine '{engine}'.")


def chunk_resources(engine: str, config: Mapping[str, Any] | None = None) -> dict[str, int]:
    program = get_program_for_engine(engine)
    values = dict(config or {})
    if program.config_model.model_config.get("extra") == "forbid":
        # Callers send the legacy `scoring_function` argument along with the config; a program
        # without that field (QuickVina, AutoDock-GPU) must not fail validation on it.
        values = {key: value for key, value in values.items() if key in program.config_model.model_fields}
    resources = program.resource_requirements(values)
    gpu = int(resources.get("gpu_required") or 0)
    return {"_gpu_required": gpu} if gpu else {}


__all__ = [
    "AUTODOCK_PROGRAM",
    "AutoDockLikeProgram",
    "DockingProgramSpec",
    "DockingEngineConfig",
    "GninaEngineConfig",
    "ADGPU_PROGRAM",
    "AutoDockGPUEngineConfig",
    "GNINA_PROGRAM",
    "GninaProgram",
    "QVINA_PROGRAM",
    "QVinaEngineConfig",
    "VINA_PROGRAM",
    "VinaProgram",
    "get_docking_program",
    "get_program_for_engine",
    "chunk_resources",
    "list_docking_programs",
    "register_docking_program",
]
