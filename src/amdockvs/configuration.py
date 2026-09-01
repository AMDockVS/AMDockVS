from __future__ import annotations

from pathlib import Path

import tomllib
from pydantic import BaseModel, ConfigDict, Field

from ms_components.ms_monitor.config import MonitorConfig
from ms_components.theme import THEMES
from ms_flow.api import PydanticConfiguration


MAX_2D_PREVIEW_HEAVY_ATOMS = "max_2d_preview_heavy_atoms"
MAX_2D_PREVIEW_HEAVY_ATOMS_PATH = f"molecule_display.{MAX_2D_PREVIEW_HEAVY_ATOMS}"
THEME_NAME_PATH = "theme.name"
FONT_BASE_PT_PATH = "theme.base_font_pt"
AMDOCKVS_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


class MoleculeDisplayConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_2d_preview_heavy_atoms: int = Field(
        ge=1,
        title="Maximum heavy atoms for 2D previews",
        description="Small molecules above this heavy-atom count are not rendered in catalog 2D previews.",
    )


class DockingDefaults(BaseModel):
    """User-settable defaults the docking panel pre-fills. Every value stays
    tuneable per-run in the UI; this only seeds the initial form."""

    model_config = ConfigDict(extra="forbid")

    exhaustiveness: int = Field(
        8, ge=1, le=256, title="Exhaustiveness", description="Default Vina/gnina search exhaustiveness."
    )
    num_modes: int = Field(9, ge=1, le=128, title="Num modes", description="Default number of poses to generate.")
    cpu_per_task: int = Field(1, ge=1, le=128, title="CPU per task", description="Default CPU cores per docking task.")
    binding_site_box_size: float = Field(
        22.0,
        ge=8.0,
        le=120.0,
        title="Binding site box (A)",
        description="Default cubic search-box edge seeded in the receptor import panel.",
    )


class BatchSizeConfiguration(BaseModel):
    """How many molecules travel in one chunk, per kind.

    Bounded by element count, not by RAM: the count is known before anything is parsed, so a run
    is reproducible and resumable, while an RSS budget is neither. Receptors are far larger per
    molecule, hence the smaller number.
    """

    model_config = ConfigDict(extra="forbid")

    ligand: int = Field(
        1000,
        ge=1,
        le=100_000,
        title="Ligands per batch",
        description="Small molecules carried in a single job chunk.",
    )
    receptor: int = Field(
        32,
        ge=1,
        le=10_000,
        title="Receptors per batch",
        description="Receptors carried in a single job chunk; lower than ligands because each is much larger.",
    )

    def for_kind(self, kind: str) -> int:
        return self.receptor if str(kind).strip().lower() == "receptor" else self.ligand


class ManagedToolConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    prefix: str = Field(
        "",
        description="Optional managed-environment prefix; empty uses AMDock's data directory.",
    )


class ProtonationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openbabel: ManagedToolConfiguration = ManagedToolConfiguration(version="3.1.1")
    pkasso: ManagedToolConfiguration = ManagedToolConfiguration(version="0.6.1")


class TableSortPref(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    descending: bool = False


class TableViewState(BaseModel):
    """Persisted per-table functional prefs (not hard config): which columns are
    visible by default and the default sort. Applied on load, captured on change."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list, description="Visible column fields; empty = table default.")
    sort: list[TableSortPref] = Field(default_factory=list, description="Default multi-column sort.")


class ThemeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        "auto",
        title="Theme",
        description="Active color theme id, or 'auto' to follow the OS color scheme.",
        # Offered as a combo, not enforced as a type: retired theme ids still have to
        # load so they can be migrated (see ui.theme).
        json_schema_extra={"choices": ["auto", *sorted(THEMES)]},
    )


class AMDockConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    molecule_display: MoleculeDisplayConfiguration
    # Component sections: the component owns the defaults (in its own code); AMDock
    # nests the model so its config file can persist overrides. See MonitorConfig.
    monitor: MonitorConfig = MonitorConfig()
    theme: ThemeConfiguration = ThemeConfiguration()
    docking: DockingDefaults = DockingDefaults()
    batch_sizes: BatchSizeConfiguration = BatchSizeConfiguration()
    protonation: ProtonationConfiguration = ProtonationConfiguration()
    # Per-table view prefs, keyed by a stable table id (the BoundTableWidget subclass name).
    tables: dict[str, TableViewState] = Field(
        default_factory=dict,
        json_schema_extra={"settings_hidden": True},  # the table writes it itself; not a setting
    )


_PACKAGED_DEFAULT = AMDockConfiguration.model_validate(
    tomllib.loads(AMDOCKVS_DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
)
DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS = _PACKAGED_DEFAULT.molecule_display.max_2d_preview_heavy_atoms
# The one base value; io/ layers derive from this so a plain-code default never drifts from config.
DEFAULT_BINDING_SITE_BOX_SIZE = _PACKAGED_DEFAULT.docking.binding_site_box_size


def batch_size_for(kind: str, runtime=None) -> int:
    """Chunk size for this molecule kind, settings first, packaged default otherwise."""
    return app_config(runtime).batch_sizes.for_kind(kind)


def create_amdock_configuration() -> PydanticConfiguration:
    return PydanticConfiguration(
        config_id="amdockvs",
        display_name="AMDockVS",
        model_type=AMDockConfiguration,
        default_path=AMDOCKVS_DEFAULT_CONFIG_PATH,
        global_path=Path.home() / ".config" / "AMDockVS" / "config.toml",
        project_relative_path=Path(".molsuite") / "config" / "amdockvs.toml",
        description="Molecule display and AMDock-specific workflow settings.",
    )


def app_config(runtime=None) -> AMDockConfiguration:
    """Effective settings as the validated model: read them as `app_config(rt).docking.num_modes`.

    Pass the runtime whenever you have one. Only *its* configuration object has the active
    project's root set, and PydanticConfiguration.set_value writes to the project layer while a
    project is open — so a standalone read (runtime=None) sees defaults + global overrides only.
    """
    configuration = getattr(runtime, "amdock_configuration", None)
    try:
        return (configuration or create_amdock_configuration()).get_value("")
    except Exception:  # noqa: BLE001 — a hand-edited config file must not take the UI down
        return _PACKAGED_DEFAULT


__all__ = [
    "AMDOCKVS_DEFAULT_CONFIG_PATH",
    "AMDockConfiguration",
    "BatchSizeConfiguration",
    "DEFAULT_BINDING_SITE_BOX_SIZE",
    "DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS",
    "DockingDefaults",
    "MAX_2D_PREVIEW_HEAVY_ATOMS",
    "MAX_2D_PREVIEW_HEAVY_ATOMS_PATH",
    "MoleculeDisplayConfiguration",
    "ManagedToolConfiguration",
    "MonitorConfig",
    "ProtonationConfiguration",
    "TableSortPref",
    "TableViewState",
    "THEME_NAME_PATH",
    "FONT_BASE_PT_PATH",
    "ThemeConfiguration",
    "app_config",
    "batch_size_for",
    "create_amdock_configuration",
]
