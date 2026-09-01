from functools import lru_cache
from importlib.resources import files

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon

from .themed import themed_icon


def _os_scheme_a() -> QColor:
    """A-color for the window/taskbar icon: follows the OS light/dark scheme, NOT the
    app theme — the taskbar is OS chrome we don't paint. Dark or Unknown -> light ink
    (most Linux taskbars are dark, and Unknown means no desktop portal to ask)."""
    app = QGuiApplication.instance()
    scheme = app.styleHints().colorScheme() if app else Qt.ColorScheme.Unknown
    return QColor("#323550") if scheme == Qt.ColorScheme.Light else QColor("#dfe3ec")


# Two-color brand logo: the "A" follows the OS scheme so it stays legible on the taskbar;
# the molecule keeps its fixed brand teal (the identity, vivid on any background).
# See logo.svg tokens `currentColor` / `themeAccent`.
_TWO_COLOR = {
    "logo.svg": (_os_scheme_a, {"themeAccent": QColor("#23cda7")}),
}

# Brand logos are recognized BY their color; tinting them monochrome destroys them.
# These bypass the themed engine and load with their own colors intact.
# ponytail: a small explicit set beats fuzzy "is this monochrome?" detection. Add a
# filename here if a multicolor asset should keep its colors.
_KEEP_COLOR = {"pymol.svg"}


# ponytail: cache the QIcon per name. The themed QIcon re-resolves its color at paint
# time and the palette watcher drops the pixmap cache on theme change, so one cached
# QIcon is correct across every theme — no need to rebuild on switch.
@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    path = str(files(__package__) / name)
    if name in _KEEP_COLOR:
        return QIcon(path)
    if name in _TWO_COLOR:
        color, extra = _TWO_COLOR[name]
        return themed_icon(path, color, extra)
    return themed_icon(path)


__all__ = ["icon"]
