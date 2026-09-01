"""Monochrome SVG icons that follow the active QPalette.

Trimmed core lifted from `scripts/themed_icons.py` (which was ~1000 lines with a
parallel Catppuccin-only palette system that duplicated `ui/theme.py`). This keeps
only what AmDock needs: one QIconEngine that resolves its color at paint time from
the app palette, plus a watcher that drops the pixmap cache when the theme changes.

`theme.py`'s `apply_theme` calls `app.setPalette(...)`, which fires
`ApplicationPaletteChange` — the watcher catches it and every icon repaints in the
new theme's color. No call site has to know a theme changed.

Color source is a `QPalette.ColorRole` (default `ButtonText`, i.e. the theme's
`text`) or a fixed `QColor`. A multicolor SVG authored with named tokens
(`currentColor`, plus any passed via `extra=`) has each token themed independently
and its other colors kept — this is how the two-color brand logo follows the theme.
A brand logo with no such tokens (e.g. pymol.svg) must NOT go through here — a single
tint flattens it; load those as a plain `QIcon`.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable, Union

from PySide6.QtCore import QByteArray, QEvent, QObject, QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap,
)
from PySide6.QtSvg import QSvgRenderer

# A ColorRole (resolved from the app palette), a fixed QColor, or a zero-arg callable
# returning a QColor — the callable is invoked at paint time, so it can track something
# outside the app palette (e.g. the OS light/dark scheme for a window/taskbar icon).
ColorSource = Union[QPalette.ColorRole, QColor, Callable[[], QColor]]


class _PaletteWatcher(QObject):
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ApplicationPaletteChange:
            ThemedIconEngine.invalidate_caches()
        return False


_watcher: _PaletteWatcher | None = None
_color_scheme_slot: Callable[..., None] | None = None


def _ensure_watcher() -> None:
    global _color_scheme_slot, _watcher
    app = QGuiApplication.instance()
    if app is not None and _watcher is None:
        _watcher = _PaletteWatcher(app)
        app.installEventFilter(_watcher)
        # OS light/dark switch doesn't fire ApplicationPaletteChange; catch it so a
        # callable ColorSource (e.g. the OS-scheme window icon) repaints.
        _color_scheme_slot = lambda *_: ThemedIconEngine.invalidate_caches()
        app.styleHints().colorSchemeChanged.connect(_color_scheme_slot)


def shutdown_themed_icons(app: QGuiApplication | None = None) -> None:
    """Detach Qt callbacks and release GUI resources before QApplication teardown."""
    global _color_scheme_slot, _watcher
    app = app or QGuiApplication.instance()
    watcher, color_scheme_slot = _watcher, _color_scheme_slot
    _watcher = None
    _color_scheme_slot = None

    if app is not None and watcher is not None:
        try:
            app.removeEventFilter(watcher)
        except RuntimeError:
            pass
    if app is not None and color_scheme_slot is not None:
        try:
            app.styleHints().colorSchemeChanged.disconnect(color_scheme_slot)
        except (RuntimeError, TypeError):
            pass

    # QPixmap and QSvgRenderer objects must not survive their QGuiApplication.
    ThemedIconEngine._pixmaps.clear()
    ThemedIconEngine._renderers.clear()


class ThemedIconEngine(QIconEngine):
    """Renders an SVG tinted to a palette color, resolved at paint time.

    A monochrome SVG (any solid `fill`) is tinted with CompositionMode_SourceIn.
    An SVG authored with `fill="currentColor"` gets the color substituted textually
    (QtSvg has no `currentColor` support), which also lets a partially themable
    multicolor SVG keep its other colors.
    """

    _MAX_PIXMAPS = 512
    _pixmaps: OrderedDict[tuple, QPixmap] = OrderedDict()   # LRU, shared
    _renderers: dict[tuple, QSvgRenderer] = {}
    _svg_sources: dict[str, tuple[str, bool]] = {}          # path -> (text, uses currentColor)
    _revision: int = 0

    def __init__(self, svg_path: str, color: ColorSource = QPalette.ColorRole.ButtonText,
                 extra: dict[str, ColorSource] | None = None):
        super().__init__()
        self._path = str(svg_path)
        self._color = color
        # Extra token -> ColorSource for a fully themable multicolor SVG (e.g. logo.svg,
        # whose "A" is `color` via currentColor and molecule is `extra["themeAccent"]`).
        self._extra = dict(extra) if extra else {}

    @classmethod
    def invalidate_caches(cls) -> None:
        cls._revision += 1
        cls._pixmaps.clear()
        # Keep the base (untinted) SVG parses; drop currentColor-substituted ones.
        cls._renderers = {k: v for k, v in cls._renderers.items() if k[1] is None}

    # -- QIconEngine ------------------------------------------------------

    def clone(self) -> QIconEngine:
        return ThemedIconEngine(self._path, self._color, self._extra)

    def key(self) -> str:
        return "ThemedIconEngine"

    def iconName(self) -> str:
        return Path(self._path).stem

    def isNull(self) -> bool:
        return not self._svg_source(self._path)[0]

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        if rect.isEmpty():
            return
        pm = self._render(rect.size(), mode, state, painter.device().devicePixelRatioF())
        if not pm.isNull():
            painter.drawPixmap(rect, pm)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        app = QGuiApplication.instance()
        return self._render(size, mode, state, app.devicePixelRatio() if app else 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        return self._render(size, mode, state, scale)

    # A vector engine can render any size. Advertising a size ladder lets the window
    # system (_NET_WM_ICON / taskbar) pick a native-resolution pixmap instead of
    # upscaling one small render — the cause of a pixelated window icon.
    _SIZE_LADDER = [QSize(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]

    def availableSizes(self, mode: QIcon.Mode = QIcon.Mode.Normal,
                       state: QIcon.State = QIcon.State.Off) -> list[QSize]:
        return list(self._SIZE_LADDER)

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        return size   # square vector: fills whatever square is asked for

    # -- internals --------------------------------------------------------

    @classmethod
    def _svg_source(cls, path: str) -> tuple[str, bool]:
        info = cls._svg_sources.get(path)
        if info is None:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError:
                text = ""
            info = (text, "currentColor" in text)
            cls._svg_sources[path] = info
        return info

    @classmethod
    def _renderer(cls, path: str, color: QColor,
                  extra: dict[str, QColor] | None = None) -> tuple[QSvgRenderer | None, bool]:
        """(renderer, needs_tint). needs_tint=True for a plain monochrome SVG."""
        text, uses_current = cls._svg_source(path)
        if not text:
            return None, False
        if uses_current:
            subs = {"currentColor": color, **(extra or {})}
            key = (path, tuple(sorted((t, c.name()) for t, c in subs.items())))
            r = cls._renderers.get(key)
            if r is None:
                # Tokens don't overlap ("themeAccent" ∌ "currentColor"), so chained
                # replace is safe regardless of order.
                for tok, col in subs.items():
                    text = text.replace(tok, col.name())
                r = QSvgRenderer(QByteArray(text.encode()))
                cls._renderers[key] = r
            return (r if r.isValid() else None), False
        key = (path, None)
        r = cls._renderers.get(key)
        if r is None:
            r = QSvgRenderer(QByteArray(text.encode()))
            cls._renderers[key] = r
        return (r if r.isValid() else None), True

    def _resolve(self, source: ColorSource, mode: QIcon.Mode, state: QIcon.State) -> QColor:
        if callable(source):                              # ColorRole/QColor aren't callable
            source = source()
        app = QGuiApplication.instance()
        pal = app.palette() if app else QPalette()
        # State.On (checked toolbutton/action) and Mode.Selected (selected item) both
        # sit on the highlight background, so both use HighlightedText — QToolButton
        # signals "checked" via State.On, item views via Mode.Selected.
        on_highlight = mode == QIcon.Mode.Selected or state == QIcon.State.On
        if isinstance(source, QPalette.ColorRole):
            if mode == QIcon.Mode.Disabled:
                return pal.color(QPalette.ColorGroup.Disabled, source)
            if on_highlight:
                return pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText)
            return pal.color(QPalette.ColorGroup.Active, source)
        color = QColor(source)                            # fixed QColor
        if mode == QIcon.Mode.Disabled:
            color.setAlphaF(color.alphaF() * 0.40)
        return color

    def _render(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, dpr: float) -> QPixmap:
        color = self._resolve(self._color, mode, state)
        extra = {tok: self._resolve(src, mode, state) for tok, src in self._extra.items()}
        cache_key = (self._path, size.width(), size.height(), round(dpr, 2),
                     int(mode.value), int(state.value), color.rgba(),
                     tuple(c.rgba() for c in extra.values()), self._revision)
        cached = self._pixmaps.get(cache_key)
        if cached is not None:
            self._pixmaps.move_to_end(cache_key)
            return cached

        renderer, needs_tint = self._renderer(self._path, color, extra)
        if renderer is None:
            return QPixmap()

        device = QSize(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
        pm = QPixmap(device)
        pm.fill(Qt.GlobalColor.transparent)
        pm.setDevicePixelRatio(dpr)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        target = QRectF(0, 0, size.width(), size.height())
        natural = QSizeF(renderer.defaultSize())
        if natural.isValid() and not natural.isEmpty():   # keep aspect ratio, centered
            scaled = natural.scaled(target.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((size.width() - scaled.width()) / 2.0,
                            (size.height() - scaled.height()) / 2.0,
                            scaled.width(), scaled.height())
        renderer.render(painter, target)

        full = QRectF(0, 0, size.width(), size.height())
        if needs_tint:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(full, color)
        elif color.alpha() < 255:                          # currentColor was opaque; apply alpha
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.fillRect(full, QColor(0, 0, 0, color.alpha()))
        painter.end()

        self._pixmaps[cache_key] = pm
        if len(self._pixmaps) > self._MAX_PIXMAPS:
            self._pixmaps.popitem(last=False)
        return pm


def themed_icon(svg_path: str, color: ColorSource = QPalette.ColorRole.ButtonText,
                extra: dict[str, ColorSource] | None = None) -> QIcon:
    """A QIcon that recolors itself whenever the app palette changes.

    `color` fills the SVG's monochrome/`currentColor` parts; `extra` maps additional
    tokens in a multicolor SVG to their own ColorSource (see logo.svg).
    """
    _ensure_watcher()
    return QIcon(ThemedIconEngine(svg_path, color, extra))


if __name__ == "__main__":
    # Self-check: same SVG tints to two different palette colors, non-null, and the
    # two renders actually differ. Runs headless.
    import os, sys, tempfile
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    svg = Path(tempfile.mkstemp(suffix=".svg")[1])
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                   '<circle cx="12" cy="12" r="10" fill="#000"/></svg>')

    eng = ThemedIconEngine(str(svg), QColor("#ff0000"))
    red = eng.pixmap(QSize(32, 32), QIcon.Mode.Normal, QIcon.State.Off)
    assert not red.isNull()
    assert red.toImage().pixelColor(16, 16).red() > 200        # tinted red

    eng2 = ThemedIconEngine(str(svg), QColor("#00ff00"))
    green = eng2.pixmap(QSize(32, 32), QIcon.Mode.Normal, QIcon.State.Off)
    assert green.toImage().pixelColor(16, 16).green() > 200     # tinted green
    assert red.toImage() != green.toImage()

    dis = eng.pixmap(QSize(32, 32), QIcon.Mode.Disabled, QIcon.State.Off)
    assert dis.toImage().pixelColor(16, 16).alpha() < 200       # dimmed

    # State.On (checked) with a palette role uses HighlightedText, not the base role,
    # so a checked toolbutton contrasts against the highlight background.
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#111111"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)
    role_eng = ThemedIconEngine(str(svg), QPalette.ColorRole.ButtonText)
    off = role_eng.pixmap(QSize(32, 32), QIcon.Mode.Normal, QIcon.State.Off)
    on = role_eng.pixmap(QSize(32, 32), QIcon.Mode.Normal, QIcon.State.On)
    assert off.toImage().pixelColor(16, 16).red() < 60          # ButtonText (dark)
    assert on.toImage().pixelColor(16, 16).red() > 200          # HighlightedText (light)

    # Two-color SVG: each named token themed independently, other colors kept.
    two = Path(tempfile.mkstemp(suffix=".svg")[1])
    two.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                   '<rect x="0" y="0" width="24" height="12" fill="currentColor"/>'
                   '<rect x="0" y="12" width="24" height="12" fill="themeAccent"/></svg>')
    two_eng = ThemedIconEngine(str(two), QColor("#ff0000"), {"themeAccent": QColor("#0000ff")})
    pm = two_eng.pixmap(QSize(24, 24), QIcon.Mode.Normal, QIcon.State.Off)
    assert pm.toImage().pixelColor(12, 6).red() > 200           # currentColor -> red
    assert pm.toImage().pixelColor(12, 18).blue() > 200         # themeAccent -> blue

    # Callable ColorSource is resolved at paint time (used for the OS-scheme window icon).
    call_eng = ThemedIconEngine(str(svg), lambda: QColor("#00ff00"))
    cpm = call_eng.pixmap(QSize(32, 32), QIcon.Mode.Normal, QIcon.State.Off)
    assert cpm.toImage().pixelColor(16, 16).green() > 200
    print("ok: themed icons tint per color, dim when disabled, flip on checked/selected, "
          "two-color tokens themed independently")
