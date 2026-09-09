"""Every tunable that used to be hardcoded is reachable from settings, and the plain-code
defaults still agree with the packaged config.

Two sources of truth for the same number is how `batch_sizes.ligand` and
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
    assert DEFAULT_LIGAND_BATCH_SIZE == config.batch_sizes.ligand
    assert DEFAULT_DOCKING_BATCH_SIZE == config.batch_sizes.docking
    assert DEFAULT_HIT_SAFETY_CAP == config.shards.hit_cap
    assert DEFAULT_TEMPERATURE_K == config.docking.temperature_k


def test_every_promoted_tunable_has_a_settings_field():
    """The sections this pass introduced, so a later edit cannot quietly drop one."""
    promoted = {
        "batch_sizes": ("docking", "output_flush_every", "import_max_inflight"),
        "shards": ("max_bytes", "suggest_bytes", "hit_cap"),
        "docking": ("temperature_k",),
        "binding_sites": ("box_scale", "box_padding", "box_min_edge", "box_max_edge", "cavity_max_burial"),
        "external_tools": ("tools_home", "vina_path", "p2rank_home"),
        "diversity": ("inline_run_limit", "molecules_per_cpu", "sample_limit"),
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

    assert app_config().external_tools.vina_path == ""
    assert str(vina_command()).strip()
    assert tools_home().is_absolute()
    assert p2rank_home().is_absolute()
