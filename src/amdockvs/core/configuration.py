from __future__ import annotations

from pathlib import Path

import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ms_components.ms_monitor.config import MonitorConfig
from ms_components.theme import THEMES
from ms_contactmap.settings import SETTINGS_PATH as CONTACT_MAP_SETTINGS_PATH
from ms_contactmap.style import (
    COLOR_MODES,
    DEFAULT_LEGEND_POSITION,
    DEFAULT_LEGEND_ROWS,
    EXPOSURE_MODES,
    GLYPH_MODES,
    LEGEND_POSITIONS,
    LEGEND_ROW_OPTIONS,
    METAL_MODES,
    PALETTES,
    SURFACE_MODES,
    DiagramStyle,
)
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


class _VinaFamilySettings(BaseModel):
    """Per-program defaults the docking panel pre-fills; every value stays tuneable per run."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field("", title="Executable", description="Path to the binary; empty autodetects.")
    exhaustiveness: int = Field(8, ge=1, le=256, title="Exhaustiveness", description="Search exhaustiveness.")
    num_modes: int = Field(9, ge=1, le=128, title="Num modes", description="Number of poses to generate.")
    cpu_per_task: int = Field(1, ge=1, le=128, title="CPU per task", description="CPU cores per docking task.")


class VinaSettings(_VinaFamilySettings):
    path: str = Field(
        "",
        title="Executable",
        description="Path to the vina binary; empty autodetects next to the interpreter, then on PATH.",
    )


class GninaSettings(_VinaFamilySettings):
    path: str = Field(
        "",
        title="Executable",
        description="Path to the gnina binary; empty uses $AMDOCK_GNINA, then PATH.",
    )


class QVinaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        "",
        title="Executable",
        description="Path to the QuickVina 2 binary; empty uses $AMDOCK_QVINA, the managed install, then PATH.",
    )


class AutoDockGPUSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        "",
        title="Executable",
        description="Path to the AutoDock-GPU binary; empty uses $AMDOCK_ADGPU, the managed install, then PATH.",
    )


class DockingConfiguration(BaseModel):
    """Settings shared by every docking program, plus one sub-section per program."""

    model_config = ConfigDict(extra="forbid")

    box_size: float = Field(
        22.0,
        ge=8.0,
        le=120.0,
        title="Binding site box (A)",
        description="Default cubic search-box edge seeded in the receptor import panel.",
    )
    temperature: float = Field(
        298.15,
        ge=100.0,
        le=500.0,
        title="Temperature (K)",
        description="Temperature used to turn a docking score into a predicted Ki/pKi.",
    )
    batch_size: int = Field(
        4,
        ge=1,
        le=1_000,
        title="Docking pairs per task",
        description=(
            "Receptor-ligand pairs carried in one docking task. Small values parallelize better; "
            "large ones amortize process startup."
        ),
    )
    vina: VinaSettings = VinaSettings()
    gnina: GninaSettings = GninaSettings()
    qvina: QVinaSettings = QVinaSettings()
    adgpu: AutoDockGPUSettings = AutoDockGPUSettings()


class PreparationConfiguration(BaseModel):
    """How many molecules travel in one preparation chunk, per kind.

    Bounded by element count, not by RAM: the count is known before anything is parsed, so a run
    is reproducible and resumable, while an RSS budget is neither. Receptors are far larger per
    molecule, hence the smaller number.
    """

    model_config = ConfigDict(extra="forbid")

    ligands_per_task: int = Field(
        1000,
        ge=1,
        le=100_000,
        title="Ligands per task",
        description="Ligand rows carried in one preparation (or other row-processing) task.",
    )
    receptors_per_task: int = Field(
        32,
        ge=1,
        le=10_000,
        title="Receptors per task",
        description="Receptors carried in a single job chunk; lower than ligands because each is much larger.",
    )

    def for_kind(self, kind: str) -> int:
        return self.receptors_per_task if str(kind).strip().lower() == "receptor" else self.ligands_per_task


class ImportConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_inflight: int = Field(
        32,
        ge=1,
        le=1_000,
        title="Import tasks in flight",
        description="Upper bound on import tasks dispatched before results are consumed.",
    )


_DIAGRAM_STYLE = DiagramStyle()


class ContactMapConfiguration(BaseModel):
    """MS-ContactMap's settings: how new 2D interaction diagrams look.

    MS-ContactMap is an app of its own, so this is not a section of AMDockVS: it is a
    separate entry of the settings dialog over MS-ContactMap's own file, the one its
    standalone window reads and writes. The defaults and allowed values come from its
    Qt-free ``style`` module.
    """

    model_config = ConfigDict(extra="forbid")

    glyphs: Literal[GLYPH_MODES] = Field(
        _DIAGRAM_STYLE.glyphs, description="Residue glyphs: side-chain shapes or plain circles."
    )
    metals: Literal[METAL_MODES] = Field(_DIAGRAM_STYLE.metals, description="Glyph drawn for metal ions.")
    coloring: Literal[COLOR_MODES] = Field(
        _DIAGRAM_STYLE.coloring,
        description="Residue colour: by chemical nature, by residue, or a blend of both (experimental).",
    )
    palette: Literal[PALETTES] = Field(_DIAGRAM_STYLE.palette, description="Colour saturation level.")
    surface: Literal[SURFACE_MODES] = Field(_DIAGRAM_STYLE.surface, description="How the pocket surface is drawn.")
    exposure: Literal[EXPOSURE_MODES] = Field(
        _DIAGRAM_STYLE.exposure, description="How the ligand's solvent exposure is drawn."
    )
    hydrophobic_lines: bool = Field(
        _DIAGRAM_STYLE.hydrophobic_lines, description="Draw hydrophobic contacts as lines."
    )
    legend_position: Literal[LEGEND_POSITIONS] = Field(DEFAULT_LEGEND_POSITION, description="Side the legend sits on.")
    legend_rows: Literal[LEGEND_ROW_OPTIONS] = Field(
        DEFAULT_LEGEND_ROWS, description="Rows of a top or bottom legend."
    )


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

    suggest_records: int = Field(
        500_000,
        ge=1,
        le=1_000_000_000,
        title="Suggest shards above (molecules)",
        description="Queued molecules above which the importer proposes sharding instead of a materialized import.",
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


class ResiduePocketSettings(BaseModel):
    """LIGSITE cavity scan behind "box from residue selection" (binding_sites.cavity)."""

    model_config = ConfigDict(extra="forbid")

    spacing: float = Field(1.0, ge=0.2, le=3.0, title="Grid spacing (A)", description="Voxel resolution: precision vs speed.")
    probe: float = Field(
        1.4, ge=0.5, le=5.0, title="Probe radius (A)",
        description="Solvent probe; larger closes the pocket and pulls the center toward the mouth.",
    )
    vdw_scale: float = Field(1.0, ge=0.5, le=2.0, title="vdW scale", description="Atom radius scale in the occupancy grid.")
    max_burial: float = Field(
        10.0, ge=1.0, le=40.0, title="Burial scan reach (A)", description="How deep the scan probes for buried space."
    )
    psp_min: int = Field(4, ge=0, le=7, title="Enclosed directions", description="Directions (0-7) enclosed to count as pocket.")
    search_radius: float = Field(
        14.0, ge=2.0, le=40.0, title="Search radius (A)", description="Radius around the selection where a pocket is sought."
    )
    center_radius: float = Field(
        8.0, ge=2.0, le=30.0, title="Center radius (A)", description="Local cavity volume averaged for the box center."
    )
    blob_radius: float = Field(
        12.0, ge=2.0, le=40.0, title="Pseudo-ligand extent (A)", description="Physical extent of the displayed point cloud."
    )
    n_points: int = Field(
        120, ge=10, le=2000, title="Pseudo-ligand points", description="Point density of the displayed pseudo-ligand."
    )


class P2RankSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str = Field(
        "",
        title="Install directory",
        description="Existing P2Rank installation to use instead of the one AMDock manages.",
    )
    profile: Literal["default", "alphafold"] = Field(
        "default", title="Profile", description="Default model: experimental structures or AlphaFold/predicted ones."
    )
    threads: int = Field(1, ge=1, le=128, title="Threads", description="Default threads per prediction job.")


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
    residues: ResiduePocketSettings = ResiduePocketSettings()
    p2rank: P2RankSettings = P2RankSettings()


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
    rows_per_task: int = Field(
        1000,
        ge=1,
        le=100_000,
        title="Molecules per task",
        description="Molecule rows carried in one diversity job task.",
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


# Keys that moved when sections were regrouped by activity: old -> new dotted path.
# Old global/project files keep loading; the value lands in its new home.
_LEGACY_KEYS = {
    "docking.binding_site_box_size": "docking.box_size",
    "docking.temperature_k": "docking.temperature",
    "docking.exhaustiveness": "docking.vina.exhaustiveness",
    "docking.num_modes": "docking.vina.num_modes",
    "docking.cpu_per_task": "docking.vina.cpu_per_task",
    "batch_sizes.docking": "docking.batch_size",
    "batch_sizes.ligand": "preparation.ligands_per_task",
    "batch_sizes.receptor": "preparation.receptors_per_task",
    "batch_sizes.import_max_inflight": "imports.max_inflight",
    "batch_sizes.output_flush_every": None,
    "batch_sizes.shard": None,
    "binding_sites.cavity_max_burial": "binding_sites.residues.max_burial",
    "external_tools.tools_home": "tools_home",
    "external_tools.vina_path": "docking.vina.path",
    "external_tools.p2rank_home": "binding_sites.p2rank.home",
}


class AMDockConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools_home: str = Field(
        "",
        title="Managed tools directory",
        description="Root for AMDock-installed tools (p2rank, protonation envs); empty uses the XDG data directory.",
    )
    molecule_display: MoleculeDisplayConfiguration
    # Component sections: the component owns the defaults (in its own code); AMDock
    # nests the model so its config file can persist overrides. See MonitorConfig.
    monitor: MonitorConfig = MonitorConfig()
    theme: ThemeConfiguration = ThemeConfiguration()
    imports: ImportConfiguration = ImportConfiguration()
    preparation: PreparationConfiguration = PreparationConfiguration()
    binding_sites: BindingSitesConfiguration = BindingSitesConfiguration()
    docking: DockingConfiguration = DockingConfiguration()
    shards: ShardStorageConfiguration = ShardStorageConfiguration()
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
DEFAULT_BINDING_SITE_BOX_SIZE = _PACKAGED_DEFAULT.docking.box_size
DEFAULT_TEMPERATURE_K = _PACKAGED_DEFAULT.docking.temperature
DEFAULT_LIGAND_BATCH_SIZE = _PACKAGED_DEFAULT.preparation.ligands_per_task
DEFAULT_DOCKING_BATCH_SIZE = _PACKAGED_DEFAULT.docking.batch_size
DEFAULT_IMPORT_MAX_INFLIGHT = _PACKAGED_DEFAULT.imports.max_inflight
# Chunks batched into one sink write. Flushing every chunk made the writer the bottleneck
# and starved the worker pool; job specs are built at import time, so this is a constant.
DEFAULT_OUTPUT_FLUSH_EVERY = 16
DEFAULT_SHARD_MAX_BYTES = _PACKAGED_DEFAULT.shards.max_bytes
DEFAULT_SHARD_SUGGEST_RECORDS = _PACKAGED_DEFAULT.shards.suggest_records
DEFAULT_HIT_SAFETY_CAP = _PACKAGED_DEFAULT.shards.hit_cap
DEFAULT_INLINE_RUN_LIMIT = _PACKAGED_DEFAULT.diversity.inline_run_limit
DEFAULT_MOLECULES_PER_CPU = _PACKAGED_DEFAULT.diversity.molecules_per_cpu
DEFAULT_DIVERSITY_SAMPLE_LIMIT = _PACKAGED_DEFAULT.diversity.sample_limit


def batch_size_for(kind: str, runtime=None) -> int:
    """Chunk size for this molecule kind, settings first, packaged default otherwise."""
    return app_config(runtime).preparation.for_kind(kind)


def create_amdock_configuration() -> PydanticConfiguration:
    return PydanticConfiguration(
        config_id="amdockvs",
        display_name="AMDockVS",
        model_type=AMDockConfiguration,
        default_path=AMDOCKVS_DEFAULT_CONFIG_PATH,
        global_path=Path.home() / ".config" / "AMDockVS" / "config.toml",
        project_relative_path=Path(".molsuite") / "config" / "amdockvs.toml",
        description="Molecule display and AMDock-specific workflow settings.",
        legacy_keys=_LEGACY_KEYS,
    )


def create_contact_map_configuration() -> PydanticConfiguration:
    return PydanticConfiguration(
        config_id="ms_contactmap",
        display_name="MS-ContactMap",
        model_type=ContactMapConfiguration,
        global_path=CONTACT_MAP_SETTINGS_PATH,
        description="Look of the 2D protein-ligand interaction diagrams.",
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
    "DEFAULT_SHARD_SUGGEST_RECORDS",
    "DEFAULT_TEMPERATURE_K",
    "DiversityConfiguration",
    "DockingConfiguration",
    "GninaSettings",
    "ImportConfiguration",
    "MAX_2D_PREVIEW_HEAVY_ATOMS",
    "MAX_2D_PREVIEW_HEAVY_ATOMS_PATH",
    "MoleculeDisplayConfiguration",
    "P2RankSettings",
    "PreparationConfiguration",
    "ManagedToolConfiguration",
    "ContactMapConfiguration",
    "MonitorConfig",
    "ProtonationConfiguration",
    "ResiduePocketSettings",
    "ShardStorageConfiguration",
    "TableSortPref",
    "TableViewState",
    "THEME_NAME_PATH",
    "FONT_BASE_PT_PATH",
    "ThemeConfiguration",
    "VinaSettings",
    "app_config",
    "batch_size_for",
    "create_amdock_configuration",
    "create_contact_map_configuration",
]
