from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from amdockvs.workflows import apply_workflow_filters


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
    requires_ligand_preparation: bool = True
    requires_receptor_preparation: bool = True

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


def get_docking_program(value: str | None) -> DockingProgramSpec:
    normalized = str(value or "").strip().lower() or VINA_PROGRAM.key
    normalized = _PROGRAM_ALIASES.get(normalized, normalized)
    program = _PROGRAMS.get(normalized)
    if program is None:
        supported = ", ".join(sorted(_PROGRAMS))
        raise ValueError(f"Unsupported docking program '{value}'. Supported programs: {supported}")
    return program


__all__ = [
    "AUTODOCK_PROGRAM",
    "AutoDockLikeProgram",
    "DockingProgramSpec",
    "GNINA_PROGRAM",
    "GninaProgram",
    "VINA_PROGRAM",
    "VinaProgram",
    "get_docking_program",
    "list_docking_programs",
]
