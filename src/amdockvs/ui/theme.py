"""Theme *persistence* for AMDock.

The themes themselves (palettes, `apply_theme`, the shared geometry QSS) live in
`ms_components.theme` so every MolSuite app shares them. Only "which theme
did this user pick" is AMDock's business, and that is all this module holds.
"""
from __future__ import annotations

from ms_components.theme import DEFAULT_FONT_PT, THEMES, apply_theme, resolve


def saved_theme_name(default: str = "auto") -> str:
    """The theme the user last picked, from AMDock's global config (or `default`).

    Read straight from the global config layer: this runs at startup before the
    runtime exists. A broken/absent config must never block theming, so any error
    falls back to `default`.
    """
    from amdockvs.configuration import THEME_NAME_PATH, create_amdock_configuration

    try:
        name = create_amdock_configuration().get_value(THEME_NAME_PATH) or default
        return name if name == "auto" or name in THEMES else resolve(name)
    except Exception:  # noqa: BLE001 — theme is cosmetic; never fail startup on it
        return default


def saved_base_font_pt(default: float = DEFAULT_FONT_PT) -> float:
    """The base font size chosen by the user, in points.

    Pinning it instead of inheriting the desktop one is deliberate: Qt's platform theme
    does not always load (under conda, `libqgtk3.so` usually fails) and then Qt falls back
    to 9pt, smaller than the rest of the user's applications. Same read path as the theme:
    global config, and any failure falls back to the default value.
    """
    from amdockvs.configuration import FONT_BASE_PT_PATH, create_amdock_configuration

    try:
        value = create_amdock_configuration().get_value(FONT_BASE_PT_PATH)
        return float(value) if value else default
    except Exception:  # noqa: BLE001 — font size is cosmetic; it never blocks start-up
        return default


def set_theme(name: str, window=None, app=None) -> None:
    """Apply a theme live and persist it.

    `window` is accepted for call-site compatibility but unused: theming is
    entirely app-level. Persisted in AMDock's global config (never the project
    layer — theme is a per-user preference).
    """
    from amdockvs.configuration import THEME_NAME_PATH, create_amdock_configuration

    del window  # theming is app-level; nothing window-specific to repaint
    selected = name if name == "auto" or name in THEMES else resolve(name)
    apply_theme(selected, app)
    try:
        # A fresh global-only configuration: set_project_root is never called, so
        # set_value writes to the global file, matching saved_theme_name's read.
        create_amdock_configuration().set_value(THEME_NAME_PATH, selected)
    except Exception:  # noqa: BLE001 — losing the persisted choice must not crash the UI
        pass
