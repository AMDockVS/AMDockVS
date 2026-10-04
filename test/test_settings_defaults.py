"""Every tunable that used to be hardcoded is reachable from settings, and the plain-code
defaults still agree with the packaged config.

Two sources of truth for the same number is how the ligand batch size and
`DEFAULT_LOAD_BATCH_SIZE` drifted apart: changing the setting did nothing because the import
path read the constant. The pure geometry helpers keep literal defaults on purpose (they must
import nothing), so this is the check that catches the day one side moves.
"""
from __future__ import annotations

import inspect

from amdockvs.binding_sites import boxes
from amdockvs.core.configuration import (
    AMDockConfiguration,
    DEFAULT_DOCKING_BATCH_SIZE,
    DEFAULT_HIT_SAFETY_CAP,
    DEFAULT_LIGAND_BATCH_SIZE,
    DEFAULT_TEMPERATURE_K,
    app_config,
)


def _defaults(fn) -> dict:
    signature = inspect.signature(fn)
    return {name: p.default for name, p in signature.parameters.items() if p.default is not inspect.Parameter.empty}


def test_pure_box_defaults_match_the_packaged_config():
    geometry = app_config().binding_sites
    edge_defaults = _defaults(boxes.box_edge_from_rg)
    assert edge_defaults["scale"] == geometry.box_scale
    assert edge_defaults["padding"] == geometry.box_padding
    assert edge_defaults["minimum"] == geometry.box_min_edge
    assert edge_defaults["maximum"] == geometry.box_max_edge
    # box_from_coords forwards the clamps rather than hiding a second set of them.
    assert _defaults(boxes.box_from_coords) == edge_defaults


def test_module_level_defaults_come_from_the_config():
    config = app_config()
    assert DEFAULT_LIGAND_BATCH_SIZE == config.preparation.ligands_per_task
    assert DEFAULT_DOCKING_BATCH_SIZE == config.docking.batch_size
    assert DEFAULT_HIT_SAFETY_CAP == config.shards.hit_cap
    assert DEFAULT_TEMPERATURE_K == config.docking.temperature


def test_every_promoted_tunable_has_a_settings_field():
    """The sections this pass introduced, so a later edit cannot quietly drop one."""
    promoted = {
        "imports": ("max_inflight",),
        "preparation": ("ligands_per_task", "receptors_per_task"),
        "shards": ("max_bytes", "suggest_records", "hit_cap"),
        "docking": ("box_size", "temperature", "batch_size", "vina", "gnina", "qvina", "adgpu"),
        "binding_sites": ("box_scale", "box_padding", "box_min_edge", "box_max_edge", "residues", "p2rank"),
        "diversity": ("inline_run_limit", "molecules_per_cpu", "rows_per_task", "sample_limit"),
    }
    config = app_config()
    for section, fields in promoted.items():
        assert section in AMDockConfiguration.model_fields, f"missing settings section: {section}"
        for field in fields:
            assert hasattr(getattr(config, section), field), f"{section}.{field} is not a setting"


def test_empty_tool_paths_still_autodetect():
    """An unset path must fall back to discovery, not to the empty string."""
    from amdockvs.binding_sites.p2rank import p2rank_home
    from amdockvs.core.constants import vina_command
    from amdockvs.core.paths import tools_home

    assert app_config().docking.vina.path == ""
    assert str(vina_command()).strip()
    assert tools_home().is_absolute()
    assert p2rank_home().is_absolute()


def test_legacy_keys_migrate_to_their_new_home(tmp_path, monkeypatch, caplog):
    """A config written before the regroup keeps its values, and they win over defaults."""
    import toml

    from amdockvs.core.configuration import create_amdock_configuration

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = tmp_path / ".config" / "AMDockVS" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(toml.dumps({
        "docking": {"exhaustiveness": 32, "temperature_k": 310.0},
        "batch_sizes": {"ligand": 7, "docking": 2, "output_flush_every": 3},
        "binding_sites": {"cavity_max_burial": 12.0},
        "external_tools": {"vina_path": "/opt/vina", "p2rank_home": "/opt/p2rank", "tools_home": "/opt/tools"},
    }))
    config = create_amdock_configuration().get_value("")
    assert config.docking.vina.exhaustiveness == 32
    assert config.docking.temperature == 310.0
    assert config.docking.batch_size == 2
    assert config.preparation.ligands_per_task == 7
    assert config.binding_sites.residues.max_burial == 12.0
    assert config.docking.vina.path == "/opt/vina"
    assert config.binding_sites.p2rank.home == "/opt/p2rank"
    notices = "\n".join(caplog.messages)
    assert "'docking.exhaustiveness' is now 'docking.vina.exhaustiveness'" in notices
    assert "'batch_sizes.output_flush_every' no longer exists" in notices
    assert "batch_sizes" not in toml.load(path)  # rewritten on load: said once
    assert config.tools_home == "/opt/tools"


def test_contact_map_is_its_own_configuration_over_its_own_file(tmp_path, monkeypatch):
    """MS-ContactMap owns defaults, allowed values and the file; AMDock only shows it."""
    import pytest
    from pydantic import ValidationError

    import amdockvs.core.configuration as configuration
    import ms_contactmap.settings as own
    from amdockvs.core.configuration import AMDockConfiguration, ContactMapConfiguration
    from ms_contactmap.style import DEFAULT_LEGEND_POSITION, DEFAULT_LEGEND_ROWS, PALETTES, DiagramStyle

    assert ContactMapConfiguration().model_dump() == {
        **DiagramStyle().to_dict(),
        "legend_position": DEFAULT_LEGEND_POSITION,
        "legend_rows": DEFAULT_LEGEND_ROWS,
    }
    with pytest.raises(ValidationError):
        ContactMapConfiguration(palette="neon")
    assert "contact_map" not in AMDockConfiguration.model_fields

    # What the standalone window saves is what the integrated entry shows.
    path = tmp_path / "MS-ContactMap" / "config.toml"
    monkeypatch.setattr(own, "SETTINGS_PATH", path)
    monkeypatch.setattr(configuration, "CONTACT_MAP_SETTINGS_PATH", path)
    own.save_view_settings({**own.load_view_settings(), "palette": "vivid", "legend_rows": 4, "hydrophobic_lines": False})
    shared = configuration.create_contact_map_configuration()
    assert (shared.config_id, shared.display_name) == ("ms_contactmap", "MS-ContactMap")
    assert shared.get_value("palette") == "vivid" and shared.get_value("legend_rows") == 4
    assert shared.get_value("hydrophobic_lines") is False
    assert {entry.path: entry for entry in shared.entries()}["palette"].choices == PALETTES
    shared.set_value("palette", "soft")
    assert own.load_view_settings()["palette"] == "soft"
