from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from amdockvs.workflows import apply_workflow_filters


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


class AutoDock4EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    num_modes: int = Field(default=9, ge=1, le=128)
    spacing: float = Field(default=0.375, gt=0.0)


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

# AutoDock4 shares the Vina PDBQT preparation but docks with the autodock4 engine
# (composed autogrid4 maps + autodock4). See docking/autodock4.py.
AUTODOCK4_PROGRAM = AutoDockLikeProgram(
    key="autodock4",
    label="AutoDock4",
    workflow_key="vina",
    preparation_engine="ad4",
    docking_engine="autodock4",
    config_model=AutoDock4EngineConfig,
    protocol_identity_keys=("num_modes", "spacing"),
)

_PROGRAMS: dict[str, DockingProgramSpec] = {
    VINA_PROGRAM.key: VINA_PROGRAM,
    GNINA_PROGRAM.key: GNINA_PROGRAM,
    AUTODOCK4_PROGRAM.key: AUTODOCK4_PROGRAM,
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
    resources = get_program_for_engine(engine).resource_requirements(config)
    gpu = int(resources.get("gpu_required") or 0)
    return {"_gpu_required": gpu} if gpu else {}


__all__ = [
    "AUTODOCK_PROGRAM",
    "AutoDockLikeProgram",
    "DockingProgramSpec",
    "DockingEngineConfig",
    "GninaEngineConfig",
    "AutoDock4EngineConfig",
    "GNINA_PROGRAM",
    "GninaProgram",
    "VINA_PROGRAM",
    "VinaProgram",
    "get_docking_program",
    "get_program_for_engine",
    "chunk_resources",
    "list_docking_programs",
    "register_docking_program",
]
