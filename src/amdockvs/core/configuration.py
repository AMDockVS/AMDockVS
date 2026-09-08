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
    temperature_k: float = Field(
        298.15,
        ge=100.0,
        le=500.0,
        title="Temperature (K)",
        description="Temperature used to turn a docking score into a predicted Ki/pKi.",
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
        title="Materialized ligands per task (VS)",
        description="Rows carried in one task for a materialized VS library.",
    )
    receptor: int = Field(
        32,
        ge=1,
        le=10_000,
        title="Receptors per batch",
        description="Receptors carried in a single job chunk; lower than ligands because each is much larger.",
    )
    docking: int = Field(
        4,
        ge=1,
        le=1_000,
        title="Docking pairs per task",
        description=(
            "Receptor-ligand pairs carried in one docking task. Small values parallelize better; "
            "large ones amortize process startup."
        ),
    )
    output_flush_every: int = Field(
        16,
        ge=1,
        le=10_000,
        title="Chunks per sink transaction",
        description=(
            "How many job chunks batch into one write. Flushing every chunk makes the writer the "
            "bottleneck and starves the worker pool."
        ),
    )
    import_max_inflight: int = Field(
        32,
        ge=1,
        le=1_000,
        title="Import tasks in flight",
        description="Upper bound on import tasks dispatched before results are consumed.",
    )
    shard: int = Field(
        1,
        ge=1,
        le=1_000,
        exclude=True,
        json_schema_extra={"settings_hidden": True},
        description="Deprecated compatibility field; one shard is always one task.",
    )

    def for_kind(self, kind: str) -> int:
        return self.receptor if str(kind).strip().lower() == "receptor" else self.ligand


class ShardStorageConfiguration(BaseModel):
    """Physical shard layout and retention policy."""

    model_config = ConfigDict(extra="forbid")

    records_per_shard: int = Field(
        1000,
        ge=1,
        le=100_000,
        title="Records per shard (HTP)",
        description=(
            "Physical records per HTP shard for inputs/outputs and any serializable info. This is independent "
            "from batch_sizes.ligand, which controls row-processing job chunks."
        ),
    )

    max_bytes: int = Field(
        256 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024 - 1,
        title="Max shard size (bytes)",
        description="Hard ceiling for one shard file; a source larger than this is split.",
    )

    suggest_bytes: int = Field(
        200 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024 - 1,
        title="Suggested shard size (bytes)",
        description="Size above which the importer proposes sharding instead of a materialized import.",
    )

    hit_cap: int = Field(
        100_000,
        ge=1,
        le=10_000_000,
        title="Hit ceiling per run",
        description=(
            "Safety ceiling on hits written by one HTP run, so a generous threshold cannot fill the disk."
        ),
    )

    keep_history: bool = Field(
        False,
        title="Keep replaced shard sets",
        description=(
            "Keep previous shard sets after a successful replacement. Off minimizes files and "
            "disk use; replacement must still commit the new set before deleting the old one."
        ),
    )


class BindingSitesConfiguration(BaseModel):
    """Geometry knobs for deriving a docking box from coordinates.

    Heuristics, not physics: the box edge is `2*(scale*Rg + padding)` clamped to
    [min_edge, max_edge]. Too tight and a pose cannot rotate; too loose and the search
    wastes exhaustiveness on empty space. Calibrate against redocking RMSD.
    """

    model_config = ConfigDict(extra="forbid")

    box_scale: float = Field(
        1.5, ge=0.5, le=5.0, title="Box scale (x Rg)", description="Multiplier applied to the radius of gyration."
    )
    box_padding: float = Field(
        4.0, ge=0.0, le=20.0, title="Box padding (A)", description="Constant margin added around the scaled radius."
    )
    box_min_edge: float = Field(
        12.0, ge=6.0, le=60.0, title="Minimum box edge (A)", description="Lower clamp for a derived box edge."
    )
    box_max_edge: float = Field(
        30.0, ge=10.0, le=120.0, title="Maximum box edge (A)", description="Upper clamp for a derived box edge."
    )
    cavity_max_burial: float = Field(
        10.0,
        ge=1.0,
        le=40.0,
        title="Cavity burial depth (A)",
        description="How deep the cavity scan probes for buried space when no reference ligand exists.",
    )


class ExternalToolsConfiguration(BaseModel):
    """Where AMDock finds third-party binaries.

    Empty means "work it out": autodetect on PATH / next to the interpreter, or use the
    managed install directory. Version pins stay in code because they are tied to a
    download checksum; what a user actually needs to override is the path.
    """

    model_config = ConfigDict(extra="forbid")

    tools_home: str = Field(
        "",
        title="Managed tools directory",
        description="Root for AMDock-installed tools; empty uses the XDG data directory.",
    )
    vina_path: str = Field(
        "",
        title="AutoDock Vina executable",
        description="Path to the vina binary; empty autodetects next to the interpreter, then on PATH.",
    )
    p2rank_home: str = Field(
        "",
        title="P2Rank install directory",
        description="Existing P2Rank installation to use instead of the one AMDock manages.",
    )


class DiversityConfiguration(BaseModel):
    """Where diversity selection stops being an inline computation and becomes a job."""

    model_config = ConfigDict(extra="forbid")

    inline_run_limit: int = Field(
        50_000,
        ge=100,
        le=5_000_000,
        title="Inline clustering limit",
        description="Scopes above this many molecules must go through the parallel job.",
    )
    molecules_per_cpu: int = Field(
        25_000,
        ge=1_000,
        le=1_000_000,
        title="Molecules per CPU",
        description="Work per core used to size the clustering job; lower asks for more cores.",
    )
    sample_limit: int = Field(
        2_000,
        ge=100,
        le=1_000_000,
        title="Preview sample size",
        description="Molecules sampled for the inline diversity preview.",
    )


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
    shards: ShardStorageConfiguration = ShardStorageConfiguration()
    binding_sites: BindingSitesConfiguration = BindingSitesConfiguration()
    external_tools: ExternalToolsConfiguration = ExternalToolsConfiguration()
    diversity: DiversityConfiguration = DiversityConfiguration()
    protonation: ProtonationConfiguration = ProtonationConfiguration()
    # Per-table view prefs, keyed by a stable table id (the BoundTableWidget subclass name).
    tables: dict[str, TableViewState] = Field(
        default_factory=dict,
        json_schema_extra={"settings_hidden": True},  # the table writes it itself; not a setting
    )


_PACKAGED_DEFAULT = AMDockConfiguration.model_validate(
    tomllib.loads(AMDOCKVS_DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
)
# The one base value each time; a module-level default (a pydantic Field, a JobSpec built at
# import time) derives from the packaged config so plain code never drifts from it. Anywhere a
# runtime is in hand, read `app_config(runtime)` instead — only that sees the project layer.
DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS = _PACKAGED_DEFAULT.molecule_display.max_2d_preview_heavy_atoms
DEFAULT_BINDING_SITE_BOX_SIZE = _PACKAGED_DEFAULT.docking.binding_site_box_size
DEFAULT_TEMPERATURE_K = _PACKAGED_DEFAULT.docking.temperature_k
DEFAULT_LIGAND_BATCH_SIZE = _PACKAGED_DEFAULT.batch_sizes.ligand
DEFAULT_DOCKING_BATCH_SIZE = _PACKAGED_DEFAULT.batch_sizes.docking
DEFAULT_OUTPUT_FLUSH_EVERY = _PACKAGED_DEFAULT.batch_sizes.output_flush_every
DEFAULT_IMPORT_MAX_INFLIGHT = _PACKAGED_DEFAULT.batch_sizes.import_max_inflight
DEFAULT_SHARD_MAX_BYTES = _PACKAGED_DEFAULT.shards.max_bytes
DEFAULT_SHARD_SUGGEST_BYTES = _PACKAGED_DEFAULT.shards.suggest_bytes
DEFAULT_HIT_SAFETY_CAP = _PACKAGED_DEFAULT.shards.hit_cap
DEFAULT_INLINE_RUN_LIMIT = _PACKAGED_DEFAULT.diversity.inline_run_limit
DEFAULT_MOLECULES_PER_CPU = _PACKAGED_DEFAULT.diversity.molecules_per_cpu
DEFAULT_DIVERSITY_SAMPLE_LIMIT = _PACKAGED_DEFAULT.diversity.sample_limit


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
    "BindingSitesConfiguration",
    "DEFAULT_BINDING_SITE_BOX_SIZE",
    "DEFAULT_DIVERSITY_SAMPLE_LIMIT",
    "DEFAULT_DOCKING_BATCH_SIZE",
    "DEFAULT_HIT_SAFETY_CAP",
    "DEFAULT_IMPORT_MAX_INFLIGHT",
    "DEFAULT_INLINE_RUN_LIMIT",
    "DEFAULT_LIGAND_BATCH_SIZE",
    "DEFAULT_MOLECULES_PER_CPU",
    "DEFAULT_MAX_2D_PREVIEW_HEAVY_ATOMS",
    "DEFAULT_OUTPUT_FLUSH_EVERY",
    "DEFAULT_SHARD_MAX_BYTES",
    "DEFAULT_SHARD_SUGGEST_BYTES",
    "DEFAULT_TEMPERATURE_K",
    "DiversityConfiguration",
    "DockingDefaults",
    "ExternalToolsConfiguration",
    "MAX_2D_PREVIEW_HEAVY_ATOMS",
    "MAX_2D_PREVIEW_HEAVY_ATOMS_PATH",
    "MoleculeDisplayConfiguration",
    "ManagedToolConfiguration",
    "MonitorConfig",
    "ProtonationConfiguration",
    "ShardStorageConfiguration",
    "TableSortPref",
    "TableViewState",
    "THEME_NAME_PATH",
    "FONT_BASE_PT_PATH",
    "ThemeConfiguration",
    "app_config",
    "batch_size_for",
    "create_amdock_configuration",
]
